package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"mime/multipart"
	"reflect"
	"regexp"
	"strings"
	"testing"

	"multirag/internal/common"
	"multirag/internal/storage"
)

// All multipart payloads and objects remain in memory; no service or listener is
// involved. The embedded interface makes any unexpected storage method panic.
type memoryAgentUploadStore struct {
	storage.Storage
	objects  map[string][]byte
	puts     int
	gets     int
	checks   int
	removes  []string
	onPut    func(int, string) error
	onCheck  func(int, string) (bool, error, bool)
	onGet    func(string, []byte) ([]byte, error)
	onRemove func(string) error
}

func newMemoryAgentUploadStore() *memoryAgentUploadStore {
	return &memoryAgentUploadStore{objects: map[string][]byte{"historical/keep": []byte("historical")}}
}

func (s *memoryAgentUploadStore) Put(bucket, key string, content []byte, _ ...string) error {
	s.puts++
	s.objects[bucket+"/"+key] = bytes.Clone(content)
	if s.onPut != nil {
		return s.onPut(s.puts, key)
	}
	return nil
}

func (s *memoryAgentUploadStore) Get(bucket, key string, _ ...string) ([]byte, error) {
	s.gets++
	content, exists := s.objects[bucket+"/"+key]
	if !exists {
		return nil, storage.ErrNotFound
	}
	if s.onGet != nil {
		return s.onGet(key, bytes.Clone(content))
	}
	return bytes.Clone(content), nil
}

func (s *memoryAgentUploadStore) ObjectExistsContext(ctx context.Context, bucket, key string) (bool, error) {
	if err := ctx.Err(); err != nil {
		return false, err
	}
	s.checks++
	if s.onCheck != nil {
		if exists, err, handled := s.onCheck(s.checks, key); handled {
			return exists, err
		}
	}
	_, exists := s.objects[bucket+"/"+key]
	return exists, nil
}

func (s *memoryAgentUploadStore) Remove(bucket, key string, _ ...string) error {
	s.removes = append(s.removes, bucket+"/"+key)
	if s.onRemove != nil {
		if err := s.onRemove(key); err != nil {
			return err
		}
	}
	delete(s.objects, bucket+"/"+key)
	return nil
}

func (s *memoryAgentUploadStore) RemoveContext(ctx context.Context, bucket, key string, tenantID ...string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	return s.Remove(bucket, key, tenantID...)
}

func agentUploadFileHeaders(t *testing.T, contents ...string) []*multipart.FileHeader {
	t.Helper()
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	for _, content := range contents {
		part, err := writer.CreateFormFile("file", "same.TXT")
		if err != nil {
			t.Fatal(err)
		}
		if _, err := part.Write([]byte(content)); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	form, err := multipart.NewReader(bytes.NewReader(body.Bytes()), writer.Boundary()).ReadForm(int64(body.Len()) + 1)
	if err != nil {
		t.Fatal(err)
	}
	// The form limit exceeds the entire encoded body, so no temporary files
	// exist. RemoveAll remains a defensive no-op for this in-memory fixture.
	t.Cleanup(func() {
		if err := form.RemoveAll(); err != nil {
			t.Error(err)
		}
	})
	return append([]*multipart.FileHeader(nil), form.File["file"]...)
}

func assertAgentUploadCompensated(t *testing.T, store *memoryAgentUploadStore) {
	t.Helper()
	if !reflect.DeepEqual(store.objects, map[string][]byte{"historical/keep": []byte("historical")}) {
		t.Fatalf("failed upload changed historical data or left runtime objects: %v", store.objects)
	}
	for _, key := range store.removes {
		if !strings.HasPrefix(key, "actor-downloads/") {
			t.Fatalf("cleanup touched an unrelated namespace: %s", key)
		}
	}
}

func TestAgentUploadRuntimeDescriptorsAndRepeatedFilenames(t *testing.T) {
	for _, contents := range [][]string{{"hello"}, {"first", "", "third"}} {
		t.Run(fmt.Sprintf("files-%d", len(contents)), func(t *testing.T) {
			store := newMemoryAgentUploadStore()
			result, code, err := uploadAgentFiles(t.Context(), "actor", agentUploadFileHeaders(t, contents...), store)
			if err != nil || code != common.CodeSuccess {
				t.Fatalf("upload failed: code=%v err=%v", code, err)
			}
			var descriptors []AgentUploadDescriptor
			if len(contents) == 1 {
				descriptor, ok := result.(AgentUploadDescriptor)
				if !ok {
					t.Fatalf("single upload returned %T", result)
				}
				descriptors = []AgentUploadDescriptor{descriptor}
			} else {
				var ok bool
				descriptors, ok = result.([]AgentUploadDescriptor)
				if !ok {
					t.Fatalf("batch upload returned %T", result)
				}
			}
			seen := map[string]bool{}
			for i, descriptor := range descriptors {
				if !regexp.MustCompile(`^[0-9a-f]{32}$`).MatchString(descriptor.ID) || seen[descriptor.ID] {
					t.Fatalf("invalid or reused upload ID: %s", descriptor.ID)
				}
				seen[descriptor.ID] = true
				if descriptor.Name != "same.TXT" || descriptor.Extension != "txt" || descriptor.Size != int64(len(contents[i])) || descriptor.CreatedBy != "actor" || descriptor.CreatedAt <= 0 || descriptor.PreviewURL != nil {
					t.Fatalf("incorrect runtime descriptor: %+v", descriptor)
				}
				if string(store.objects["actor-downloads/"+descriptor.ID]) != contents[i] {
					t.Fatal("persisted payload differs from multipart content")
				}
				var restored AgentUploadDescriptor
				metadata := store.objects["actor-downloads/"+descriptor.ID+".upload.json"]
				if err := json.Unmarshal(metadata, &restored); err != nil || restored != descriptor {
					t.Fatalf("descriptor readback differs: %s, %v", metadata, err)
				}
				if !bytes.Contains(metadata, []byte(`"preview_url":null`)) {
					t.Fatal("descriptor omitted preview_url:null")
				}
			}
			if store.gets != len(contents)*2 || len(store.objects) != 1+len(contents)*2 || len(store.removes) != 0 {
				t.Fatalf("unexpected persistence/readback: %+v", store)
			}
		})
	}
}

func TestAgentUploadRejectsWholeInvalidBatchBeforeStorage(t *testing.T) {
	for _, invalid := range []string{"none", "nil", "empty-name", "blank-name", "long-name", "long-unicode-name", "negative-size", "overflow-size", "short-size", "long-size"} {
		t.Run(invalid, func(t *testing.T) {
			files := agentUploadFileHeaders(t, "valid", "invalid")
			switch invalid {
			case "none":
				files = nil
			case "nil":
				files[1] = nil
			case "empty-name":
				files[1].Filename = ""
			case "blank-name":
				files[1].Filename = "  "
			case "long-name":
				files[1].Filename = strings.Repeat("a", 256)
			case "long-unicode-name":
				files[1].Filename = strings.Repeat("文", 86)
			case "negative-size":
				files[1].Size = -1
			case "overflow-size":
				files[1].Size = math.MaxInt64
			case "short-size":
				files[1].Size--
			case "long-size":
				files[1].Size++
			}
			store := newMemoryAgentUploadStore()
			result, code, err := uploadAgentFiles(t.Context(), "actor", files, store)
			if result != nil || code != common.CodeArgumentError || err == nil {
				t.Fatalf("invalid batch succeeded: %v, %v, %v", result, code, err)
			}
			if store.puts != 0 || store.checks != 0 || store.gets != 0 || len(store.removes) != 0 {
				t.Fatal("invalid batch reached storage")
			}
			assertAgentUploadCompensated(t, store)
		})
	}
}

func TestAgentUploadAllowsFilenameByteLimitAndDefaultMIMEType(t *testing.T) {
	files := agentUploadFileHeaders(t, "content")
	files[0].Filename = strings.Repeat("文", 85)
	files[0].Header.Del("Content-Type")
	store := newMemoryAgentUploadStore()
	result, code, err := uploadAgentFiles(t.Context(), "actor", files, store)
	if err != nil || code != common.CodeSuccess {
		t.Fatalf("255-byte filename rejected: %v %v", code, err)
	}
	descriptor := result.(AgentUploadDescriptor)
	if descriptor.Name != files[0].Filename || descriptor.MIMEType != "application/octet-stream" {
		t.Fatalf("filename or default MIME type changed: %+v", descriptor)
	}
}

func TestAgentUploadRequiresStrictStorageBeforeWriting(t *testing.T) {
	for name, store := range map[string]storage.Storage{"nil": nil, "legacy-only": struct{ storage.Storage }{}} {
		t.Run(name, func(t *testing.T) {
			result, code, err := uploadAgentFiles(t.Context(), "actor", agentUploadFileHeaders(t, "content"), store)
			if result != nil || code != common.CodeServerError || err == nil {
				t.Fatalf("unverifiable storage accepted: %v, %v, %v", result, code, err)
			}
		})
	}
}

func TestAgentUploadReservationFailureNeverWritesOrDeletes(t *testing.T) {
	probeFailure := errors.New("probe unavailable")
	for _, mode := range []string{"unavailable", "occupied"} {
		t.Run(mode, func(t *testing.T) {
			store := newMemoryAgentUploadStore()
			store.onCheck = func(int, string) (bool, error, bool) {
				if mode == "unavailable" {
					return false, probeFailure, true
				}
				return true, nil, true
			}
			_, code, err := uploadAgentFiles(t.Context(), "actor", agentUploadFileHeaders(t, "content"), store)
			if err == nil || code != common.CodeServerError || mode == "unavailable" && !errors.Is(err, probeFailure) {
				t.Fatalf("reservation failure lost: %v, %v", code, err)
			}
			if store.puts != 0 || len(store.removes) != 0 {
				t.Fatal("unreserved upload wrote or removed objects")
			}
			assertAgentUploadCompensated(t, store)
		})
	}
}

func TestAgentUploadLateWriteFailureCompensatesWholeBatch(t *testing.T) {
	writeFailure := errors.New("late write failure")
	for failAt := 1; failAt <= 4; failAt++ {
		t.Run(fmt.Sprintf("put-%d", failAt), func(t *testing.T) {
			store := newMemoryAgentUploadStore()
			store.onPut = func(call int, _ string) error {
				if call == failAt {
					return writeFailure
				}
				return nil
			}
			result, code, err := uploadAgentFiles(t.Context(), "actor", agentUploadFileHeaders(t, "first", "second"), store)
			if result != nil || code != common.CodeServerError || !errors.Is(err, writeFailure) || errors.Is(err, ErrAgentUploadCleanup) {
				t.Fatalf("late write failure lost: %v, %v, %v", result, code, err)
			}
			if len(store.removes) != ((failAt+1)/2)*2 {
				t.Fatalf("did not compensate every attempted pair: %v", store.removes)
			}
			assertAgentUploadCompensated(t, store)
		})
	}
}

func TestAgentUploadCancellationDrainsAndCompensates(t *testing.T) {
	for cancelAt := 0; cancelAt <= 4; cancelAt++ {
		t.Run(fmt.Sprintf("after-put-%d", cancelAt), func(t *testing.T) {
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			store := newMemoryAgentUploadStore()
			if cancelAt == 0 {
				cancel()
			}
			store.onPut = func(call int, _ string) error {
				if call == cancelAt {
					cancel()
				}
				return nil
			}
			result, code, err := uploadAgentFiles(ctx, "actor", agentUploadFileHeaders(t, "first", "second"), store)
			if result != nil || code != common.CodeServerError || !errors.Is(err, context.Canceled) || errors.Is(err, ErrAgentUploadCleanup) {
				t.Fatalf("cancelled upload succeeded or cleanup inherited cancellation: %v, %v, %v", result, code, err)
			}
			if store.puts != cancelAt {
				t.Fatalf("write continued after cancellation: %d", store.puts)
			}
			assertAgentUploadCompensated(t, store)
		})
	}
}

func TestAgentUploadReadbackFailureCompensates(t *testing.T) {
	for _, mode := range []string{"corrupt", "get-error", "missing", "probe-error"} {
		t.Run(mode, func(t *testing.T) {
			store := newMemoryAgentUploadStore()
			store.onGet = func(_ string, data []byte) ([]byte, error) {
				if mode == "get-error" {
					return nil, errors.New("read unavailable")
				}
				if mode == "corrupt" {
					return []byte("wrong bytes"), nil
				}
				return data, nil
			}
			store.onCheck = func(call int, _ string) (bool, error, bool) {
				if call == 3 && mode == "missing" {
					return false, nil, true
				}
				if call == 3 && mode == "probe-error" {
					return false, errors.New("verification unavailable"), true
				}
				return false, nil, false
			}
			result, code, err := uploadAgentFiles(t.Context(), "actor", agentUploadFileHeaders(t, "content"), store)
			if result != nil || code != common.CodeServerError || err == nil || errors.Is(err, ErrAgentUploadCleanup) {
				t.Fatalf("readback failure succeeded: %v, %v, %v", result, code, err)
			}
			assertAgentUploadCompensated(t, store)
		})
	}
}

func TestAgentUploadCleanupFailurePreservesOriginalAndReadsBackEveryKey(t *testing.T) {
	writeFailure, removeFailure, probeFailure := errors.New("write failure"), errors.New("delete failure"), errors.New("cleanup probe failure")
	store := newMemoryAgentUploadStore()
	store.onPut = func(call int, _ string) error {
		if call == 2 {
			return writeFailure
		}
		return nil
	}
	store.onRemove = func(string) error { return removeFailure }
	store.onCheck = func(call int, _ string) (bool, error, bool) {
		if call > 3 {
			return false, probeFailure, true
		}
		return false, nil, false
	}
	result, code, err := uploadAgentFiles(t.Context(), "actor", agentUploadFileHeaders(t, "content"), store)
	if result != nil || code != common.CodeServerError || !errors.Is(err, writeFailure) || !errors.Is(err, removeFailure) || !errors.Is(err, probeFailure) || !errors.Is(err, ErrAgentUploadCleanup) {
		t.Fatalf("cleanup failure hid business error or readback failure: %v", err)
	}
	if len(store.removes) != 2 || store.checks != 5 || len(store.objects) != 3 {
		t.Fatalf("cleanup stopped before every object was attempted/read: %+v", store)
	}
}
