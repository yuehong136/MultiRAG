package service

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

func (s *memoryAgentUploadStore) GetContext(ctx context.Context, bucket, key string, tenant ...string) ([]byte, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return s.Get(bucket, key, tenant...)
}

func TestAgentDownloadAuthority(t *testing.T) {
	id := strings.Repeat("a", 32)
	metadata := AgentUploadDescriptor{ID: id, CreatedBy: "alice", Name: "x.html", MIMEType: "text/html", Extension: "html", Size: 3, CreatedAt: 1}
	raw, err := json.Marshal(metadata)
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name, actor, id string
		change          func(*memoryAgentUploadStore)
		unavailable     bool
	}{
		{"owner", "alice", id, nil, false},
		{"foreign", "bob", id, nil, true},
		{"arbitrary-location", "alice", "../" + id, nil, true},
		{"upper-case", "alice", strings.ToUpper(id), nil, true},
		{"descriptor-missing", "alice", id, func(s *memoryAgentUploadStore) { delete(s.objects, "alice-downloads/"+id+".upload.json") }, true},
		{"payload-missing", "alice", id, func(s *memoryAgentUploadStore) { delete(s.objects, "alice-downloads/"+id) }, true},
		{"descriptor-other-owner", "alice", id, func(s *memoryAgentUploadStore) {
			s.objects["alice-downloads/"+id+".upload.json"] = []byte(strings.ReplaceAll(string(raw), "alice", "bob"))
		}, true},
		{"descriptor-incomplete", "alice", id, func(s *memoryAgentUploadStore) {
			s.objects["alice-downloads/"+id+".upload.json"] = []byte(`{"id":"` + id + `","created_by":"alice"}`)
		}, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			store := newMemoryAgentUploadStore()
			store.objects["alice-downloads/"+id] = []byte("abc")
			store.objects["alice-downloads/"+id+".upload.json"] = raw
			if test.change != nil {
				test.change(store)
			}
			blob, err := downloadAgentFile(t.Context(), test.actor, test.id, store)
			if test.unavailable {
				if !errors.Is(err, ErrAgentFileUnavailable) || blob != nil {
					t.Fatalf("expected unavailable, got %q %v", blob, err)
				}
			} else if err != nil || string(blob) != "abc" {
				t.Fatalf("download: %q %v", blob, err)
			}
			if store.puts != 0 || len(store.removes) != 0 {
				t.Fatal("download mutated storage")
			}
			if test.id != id && (store.gets != 0 || store.checks != 0) {
				t.Fatal("invalid ID reached storage")
			}
		})
	}
}

func TestAgentDownloadStorageFailures(t *testing.T) {
	store := newMemoryAgentUploadStore()
	store.onCheck = func(int, string) (bool, error, bool) { return false, errors.New("offline"), true }
	if _, err := downloadAgentFile(t.Context(), "alice", strings.Repeat("a", 32), store); err == nil || errors.Is(err, ErrAgentFileUnavailable) {
		t.Fatal("transport failure masked", err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if _, err := downloadAgentFile(ctx, "alice", strings.Repeat("a", 32), store); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if _, err := downloadAgentFile(t.Context(), "alice", strings.Repeat("a", 32), nil); err == nil {
		t.Fatal("nil storage succeeded")
	}
}
