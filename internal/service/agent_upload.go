package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"mime/multipart"
	"multirag/internal/dao"
	"os"
	"strconv"
	"strings"
	"time"

	"multirag/internal/common"
	"multirag/internal/storage"
)

// ErrAgentUploadCleanup means a failed upload has unconfirmed object cleanup.
// The original failure remains available through errors.Is/errors.As.
var ErrAgentUploadCleanup = errors.New("runtime upload cleanup could not be confirmed")

// The Python runtime gateway uses FILE_NAME_LEN_LIMIT in UTF-8 bytes.
const agentUploadFilenameLimit = 255

// AgentUploadDescriptor matches the server-written Python runtime descriptor.
type AgentUploadDescriptor struct {
	ID         string  `json:"id"`
	Name       string  `json:"name"`
	Size       int64   `json:"size"`
	Extension  string  `json:"extension"`
	MIMEType   string  `json:"mime_type"`
	CreatedBy  string  `json:"created_by"`
	CreatedAt  float64 `json:"created_at"`
	PreviewURL *string `json:"preview_url"`
}

type agentUploadObjectChecker interface {
	ObjectExistsContext(context.Context, string, string) (bool, error)
}

type agentUploadObjectRemover interface {
	RemoveContext(context.Context, string, string, ...string) error
}

type preparedAgentUpload struct {
	descriptor AgentUploadDescriptor
	content    []byte
	metadata   []byte
}

// UploadFiles authorizes the canvas before accessing multipart data or storage.
// TEAM callers use their own runtime namespace, as do their subsequent runs.
func (s *AgentService) UploadFiles(ctx context.Context, userID, canvasID string, files []*multipart.FileHeader) (any, common.ErrorCode, error) {
	if _, code, err := s.requireCanvasAccess(ctx, userID, canvasID); err != nil {
		return nil, code, err
	}
	// Match the existing runtime upload document quota without relying on
	// Document.CreatedBy, which may differ from the knowledgebase tenant.
	limit, err := strconv.ParseInt(os.Getenv("MAX_FILE_NUM_PER_USER"), 10, 64)
	if os.Getenv("MAX_FILE_NUM_PER_USER") == "" {
		limit, err = 0, nil
	}
	if err != nil {
		return nil, common.CodeServerError, fmt.Errorf("invalid runtime upload quota configuration")
	}
	if limit > 0 {
		count, err := dao.NewUserCanvasVersionDAO().CountTenantDocuments(ctx, userID)
		if err != nil {
			return nil, common.CodeServerError, fmt.Errorf("unable to check runtime upload quota")
		}
		if count >= limit {
			return nil, common.CodeOperatingError, fmt.Errorf("Exceed the maximum file number of a free user!")
		}
	}
	return uploadAgentFiles(ctx, userID, files, storage.ForRuntimeUploads(storage.GetStorageFactory().GetStorage()))
}

func uploadAgentFiles(ctx context.Context, userID string, files []*multipart.FileHeader, store storage.Storage) (any, common.ErrorCode, error) {
	if err := ctx.Err(); err != nil {
		return nil, common.CodeServerError, err
	}
	if userID == "" || len(files) == 0 {
		return nil, common.CodeArgumentError, fmt.Errorf("provide at least one file and an authenticated user")
	}
	// Validate the entire batch before opening files or touching storage. Use a
	// bounded read below rather than allocating from an untrusted declared size.
	for _, file := range files {
		if file == nil || strings.TrimSpace(file.Filename) == "" || len(file.Filename) > agentUploadFilenameLimit || file.Size < 0 || file.Size == math.MaxInt64 {
			return nil, common.CodeArgumentError, fmt.Errorf("every file must have a filename and a valid size")
		}
	}
	if store == nil {
		return nil, common.CodeServerError, fmt.Errorf("runtime upload storage is unavailable")
	}
	checker, ok := store.(agentUploadObjectChecker)
	if !ok {
		return nil, common.CodeServerError, fmt.Errorf("runtime upload storage cannot confirm object absence")
	}
	prepared := make([]preparedAgentUpload, len(files))
	for i, file := range files {
		if err := ctx.Err(); err != nil {
			return nil, common.CodeServerError, err
		}
		source, err := file.Open()
		if err != nil {
			return nil, common.CodeArgumentError, fmt.Errorf("open uploaded file: %w", err)
		}
		content, readErr := io.ReadAll(io.LimitReader(source, file.Size+1))
		closeErr := source.Close()
		if err := errors.Join(readErr, closeErr); err != nil {
			return nil, common.CodeArgumentError, fmt.Errorf("read uploaded file: %w", err)
		}
		if int64(len(content)) != file.Size {
			return nil, common.CodeArgumentError, fmt.Errorf("uploaded file size does not match its content")
		}
		extension := strings.Split(file.Filename, ".")
		mimeType := file.Header.Get("Content-Type")
		if mimeType == "" {
			mimeType = "application/octet-stream"
		}
		prepared[i] = preparedAgentUpload{content: content, descriptor: AgentUploadDescriptor{
			Name: file.Filename, Size: int64(len(content)), Extension: strings.ToLower(extension[len(extension)-1]),
			MIMEType: mimeType, CreatedBy: userID, CreatedAt: float64(time.Now().UnixNano()) / float64(time.Second),
		}}
	}
	bucket := userID + "-downloads"
	allocated := make(map[string]struct{}, len(prepared))
	// Fresh UUID keys are checked, never reused after a failed write. Checking
	// both objects also protects historical descriptors whose payload is gone.
	for i := range prepared {
		id, err := allocateAgentUploadID(ctx, bucket, checker, allocated)
		if err != nil {
			return nil, common.CodeServerError, err
		}
		prepared[i].descriptor.ID = id
		metadata, err := json.Marshal(prepared[i].descriptor)
		if err != nil {
			return nil, common.CodeServerError, fmt.Errorf("encode runtime upload descriptor: %w", err)
		}
		prepared[i].metadata = metadata
	}
	var attempted []string
	fail := func(cause error) (any, common.ErrorCode, error) {
		return nil, common.CodeServerError, compensateAgentUploads(ctx, store, checker, bucket, attempted, cause)
	}
	for _, upload := range prepared {
		if err := ctx.Err(); err != nil {
			return fail(err)
		}
		id := upload.descriptor.ID
		// A failed Put may already have persisted bytes. Register the pair
		// before the first Put and compensate it together with prior files.
		attempted = append(attempted, id, id+".upload.json")
		for _, object := range []struct {
			key     string
			content []byte
		}{{id, upload.content}, {id + ".upload.json", upload.metadata}} {
			if err := ctx.Err(); err != nil {
				return fail(err)
			}
			// The storage API is synchronous. Let each write finish before
			// observing cancellation, so no late write escapes compensation.
			if err := store.Put(bucket, object.key, object.content); err != nil {
				return fail(fmt.Errorf("store runtime upload: %w", err))
			}
			if err := ctx.Err(); err != nil {
				return fail(err)
			}
			exists, err := checker.ObjectExistsContext(ctx, bucket, object.key)
			if err != nil {
				return fail(fmt.Errorf("verify runtime upload existence: %w", err))
			}
			if !exists {
				return fail(fmt.Errorf("runtime upload was not persisted"))
			}
			actual, err := store.Get(bucket, object.key)
			if err != nil {
				return fail(fmt.Errorf("verify runtime upload bytes: %w", err))
			}
			if !bytes.Equal(actual, object.content) {
				return fail(fmt.Errorf("runtime upload byte verification failed"))
			}
			if err := ctx.Err(); err != nil {
				return fail(err)
			}
		}
	}
	if err := ctx.Err(); err != nil {
		return fail(err)
	}
	descriptors := make([]AgentUploadDescriptor, len(prepared))
	for i, upload := range prepared {
		descriptors[i] = upload.descriptor
	}
	if len(descriptors) == 1 {
		return descriptors[0], common.CodeSuccess, nil
	}
	return descriptors, common.CodeSuccess, nil
}

func allocateAgentUploadID(ctx context.Context, bucket string, checker agentUploadObjectChecker, allocated map[string]struct{}) (string, error) {
	for attempt := 0; attempt < 8; attempt++ {
		if err := ctx.Err(); err != nil {
			return "", err
		}
		id := common.GenerateUUID()
		if _, used := allocated[id]; used {
			continue
		}
		free := true
		for _, key := range []string{id, id + ".upload.json"} {
			exists, err := checker.ObjectExistsContext(ctx, bucket, key)
			if err != nil {
				return "", fmt.Errorf("reserve runtime upload key: %w", err)
			}
			if exists {
				free = false
				break
			}
		}
		if free {
			allocated[id] = struct{}{}
			return id, nil
		}
	}
	return "", fmt.Errorf("unable to allocate a fresh runtime upload key")
}

func compensateAgentUploads(ctx context.Context, store storage.Storage, checker agentUploadObjectChecker, bucket string, keys []string, cause error) error {
	if len(keys) == 0 {
		return cause
	}
	cleanupCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()
	var failures []error
	for _, key := range keys {
		var err error
		if remover, ok := store.(agentUploadObjectRemover); ok {
			err = remover.RemoveContext(cleanupCtx, bucket, key)
		} else {
			err = store.Remove(bucket, key)
		}
		if err != nil {
			failures = append(failures, fmt.Errorf("remove runtime upload object: %w", err))
		}
		// Read back every deletion independently, including failed deletes.
		exists, err := checker.ObjectExistsContext(cleanupCtx, bucket, key)
		if err != nil {
			failures = append(failures, fmt.Errorf("confirm runtime upload cleanup: %w", err))
		} else if exists {
			failures = append(failures, fmt.Errorf("runtime upload object remains after cleanup"))
		}
	}
	if len(failures) != 0 {
		return errors.Join(cause, ErrAgentUploadCleanup, errors.Join(failures...))
	}
	return cause
}
