package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"time"

	"multirag/internal/storage"
)

// ErrAgentFileUnavailable does not disclose whether an upload belongs to another user.
var ErrAgentFileUnavailable = errors.New("attachment is missing or unavailable to this user")

var runtimeUploadID = regexp.MustCompile(`^[0-9a-f]{32}$`)

// DownloadAgentFile restores server-written authority from the caller's namespace.
// A request can name an upload ID, never an object location or another owner.
func (s *FileService) DownloadAgentFile(ctx context.Context, userID, fileID string) ([]byte, error) {
	return downloadAgentFile(ctx, userID, fileID, storage.ForRuntimeUploads(storage.GetStorageFactory().GetStorage()))
}

func downloadAgentFile(ctx context.Context, userID, fileID string, store storage.Storage) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if userID == "" || !runtimeUploadID.MatchString(fileID) {
		return nil, ErrAgentFileUnavailable
	}
	checker, ok := store.(agentUploadObjectChecker)
	if !ok || store == nil {
		return nil, fmt.Errorf("runtime upload storage is unavailable")
	}
	reader, ok := store.(storage.ContextReader)
	if !ok {
		return nil, fmt.Errorf("runtime upload storage cannot read with cancellation")
	}
	bucket := userID + "-downloads"
	for _, key := range []string{fileID + ".upload.json", fileID} {
		exists, err := checker.ObjectExistsContext(ctx, bucket, key)
		if err != nil {
			return nil, fmt.Errorf("check runtime upload: %w", err)
		}
		if !exists {
			return nil, ErrAgentFileUnavailable
		}
	}
	raw, err := reader.GetContext(ctx, bucket, fileID+".upload.json")
	if errors.Is(err, storage.ErrNotFound) {
		return nil, ErrAgentFileUnavailable
	}
	if err != nil {
		return nil, fmt.Errorf("read runtime upload descriptor: %w", err)
	}
	// Pointer fields distinguish missing metadata from legitimate zero values.
	var descriptor struct {
		ID        string   `json:"id"`
		CreatedBy string   `json:"created_by"`
		Name      *string  `json:"name"`
		MIMEType  *string  `json:"mime_type"`
		Extension *string  `json:"extension"`
		Size      *int64   `json:"size"`
		CreatedAt *float64 `json:"created_at"`
		Preview   any      `json:"preview_url"`
	}
	if err := json.Unmarshal(raw, &descriptor); err != nil || descriptor.ID != fileID || descriptor.CreatedBy != userID || descriptor.Name == nil || descriptor.MIMEType == nil || descriptor.Extension == nil || descriptor.Size == nil || *descriptor.Size < 0 || descriptor.CreatedAt == nil || descriptor.Preview != nil {
		return nil, ErrAgentFileUnavailable
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	blob, err := reader.GetContext(ctx, bucket, fileID)
	if errors.Is(err, storage.ErrNotFound) || errors.Is(err, storage.ErrBucketNotFound) {
		return nil, ErrAgentFileUnavailable
	}
	if err != nil {
		return nil, fmt.Errorf("read runtime upload: %w", err)
	}
	if int64(len(blob)) != *descriptor.Size {
		return nil, fmt.Errorf("runtime upload size does not match its descriptor")
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return blob, nil
}
