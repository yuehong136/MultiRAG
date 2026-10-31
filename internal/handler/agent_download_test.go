package handler

import (
	"context"
	"encoding/json"
	"github.com/gin-gonic/gin"
	"multirag/internal/entity"
	"multirag/internal/service"
	"multirag/internal/storage"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type downloadStore struct {
	storage.Storage
	objects map[string][]byte
	calls   int
}

func (s *downloadStore) Get(bucket, key string, _ ...string) ([]byte, error) {
	s.calls++
	b, ok := s.objects[bucket+"/"+key]
	if !ok {
		return nil, storage.ErrNotFound
	}
	return b, nil
}
func (s *downloadStore) ObjectExistsContext(_ context.Context, bucket, key string) (bool, error) {
	s.calls++
	_, ok := s.objects[bucket+"/"+key]
	return ok, nil
}
func TestAgentDownloadHTTP(t *testing.T) {
	gin.SetMode(gin.TestMode)
	id := strings.Repeat("a", 32)
	raw, _ := json.Marshal(service.AgentUploadDescriptor{ID: id, CreatedBy: "alice", Name: "active.html", MIMEType: "text/html", Extension: "html", Size: 3, CreatedAt: 1})
	store := &downloadStore{objects: map[string][]byte{"alice-downloads/" + id: []byte("abc"), "alice-downloads/" + id + ".upload.json": raw}}
	factory := storage.GetStorageFactory()
	previous := factory.GetStorage()
	factory.SetStorage(store)
	t.Cleanup(func() { factory.SetStorage(previous) })
	for _, test := range []struct {
		name, actor, query string
		status             int
	}{{"owner", "alice", "id=" + id, 200}, {"foreign", "bob", "id=" + id, 404}, {"location", "alice", "id=../file", 404}, {"duplicate", "alice", "id=" + id + "&id=" + id, 200}, {"no-user", "", "id=" + id, 200}} {
		t.Run(test.name, func(t *testing.T) {
			w := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(w)
			c.Request = httptest.NewRequest(http.MethodGet, "/api/v1/agents/download?"+test.query, nil)
			if test.actor != "" {
				c.Set("user", &entity.User{ID: test.actor})
			}
			NewAgentHandler(service.NewAgentService()).DownloadAgentFile(c)
			if w.Code != test.status || w.Header().Get("Cache-Control") != "no-store" || w.Header().Get("X-Content-Type-Options") != "nosniff" {
				t.Fatal(w.Code, w.Header(), w.Body.String())
			}
			if test.name == "owner" {
				if w.Body.String() != "abc" || w.Header().Get("Content-Type") != "application/octet-stream" {
					t.Fatal(w.Header(), w.Body.String())
				}
			} else if w.Body.String() == "abc" {
				t.Fatal("denial leaked bytes")
			}
		})
	}
}
