package handler

import (
	"github.com/gin-gonic/gin"
	"multirag/internal/entity"
	"multirag/internal/service"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestFileCommitRoutesAndStrictPayload(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	g := r.Group("/api/v1")
	g.GET("/datasets/:dataset_id", func(c *gin.Context) {})
	g.GET("/files/:id", func(c *gin.Context) {})
	g.Use(func(c *gin.Context) {
		if c.GetHeader("Authorization") == "owner" {
			c.Set("user", &entity.User{ID: "owner"})
		}
	})
	RegisterFileCommitRoutes(g, NewFileCommitHandler(service.NewFileCommitService()))
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest("GET", "/api/v1/folders/f/commits", nil))
	if !strings.Contains(w.Body.String(), "User not found") {
		t.Fatal(w.Body.String())
	}
	for _, body := range []string{`{"message":"x","files":[],"tenant_id":"other"}`, `{"message":"x","files":[{"file_id":"f","operation":"delete","unknown":true}]}`, `{"message":null,"files":[]}`, `{"message":"x","files":null}`, `{"files":[]}`, `{} {}`} {
		req := httptest.NewRequest("POST", "/api/v1/folders/f/commits", strings.NewReader(body))
		req.Header.Set("Authorization", "owner")
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != 422 {
			t.Fatalf("expected422 got%d %s", w.Code, w.Body.String())
		}
	}
}

func TestFileCommitFullStateDiffAndTree(t *testing.T) {
	old := entity.SnapshotState{"root": {Name: "root", ParentID: "root", Type: "folder", Status: "1"}, "folder": {Name: "old", ParentID: "root", Type: "folder", Status: "1"}, "unchanged": {Name: "a", ParentID: "folder", Type: "file", Hash: "hash", Size: 1, Status: "1"}}
	next := entity.SnapshotState{}
	for k, v := range old {
		next[k] = v
	}
	next["added"] = entity.SnapshotEntry{Name: "b", ParentID: "root", Type: "file", Hash: "b", Size: 1, Status: "1"}
	diff := service.DiffSnapshotStates(old, next)
	if len(diff) != 1 || diff[0].FileID != "added" || diff[0].Operation != "add" {
		t.Fatalf("false deletion in diff: %+v", diff)
	}
	e := next["folder"]
	e.Name = "new"
	next["folder"] = e
	tree, err := service.SnapshotTree(old, "root")
	if err != nil || tree["name"] != "root" {
		t.Fatal(tree, err)
	}
	delete(next, "folder")
	if _, err := service.SnapshotTree(next, "root"); err == nil {
		t.Fatal("accepted broken ancestry")
	}
}
