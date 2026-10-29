package router

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/handler"
)

func TestAfterAABDocumentPluginAndDifyRoutes(t *testing.T) {
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	r := &Router{authHandler: handler.NewAuthHandler()}
	r.Setup(app)
	routes := map[string]string{}
	for _, route := range app.Routes() {
		routes[route.Method+" "+route.Path] = route.Handler
	}
	for route, method := range map[string]string{
		"GET /api/v1/documents/:id":                               ".GetDocumentByID-fm",
		"GET /api/v1/documents/:id/preview":                       ".GetDocumentPreview-fm",
		"GET /api/v1/documents/:id/download":                      ".DownloadDocumentFile-fm",
		"GET /api/v1/documents/artifact/:filename":                ".GetDocumentArtifact-fm",
		"GET /api/v1/datasets/:dataset_id/documents/:document_id": ".DownloadDocument-fm",
		"GET /api/v1/plugin/tools":                                ".ListLLMTools-fm",
		"POST /api/v1/dify/retrieval":                             ".Retrieval-fm",
	} {
		if !strings.HasSuffix(routes[route], method) {
			t.Fatalf("route %s must bind %s, got %s", route, method, routes[route])
		}
	}
	for _, path := range []string{
		"/api/v1/documents/document/preview", "/api/v1/documents/document/download",
		"/api/v1/documents/artifact/file.txt", "/api/v1/datasets/dataset/documents/document",
	} {
		response := httptest.NewRecorder()
		app.ServeHTTP(response, httptest.NewRequest(http.MethodGet, path, nil))
		wantCache := "no-store"
		if strings.HasPrefix(path, "/api/v1/datasets/") {
			wantCache = "private, no-store"
		}
		if response.Code != http.StatusUnauthorized || response.Header().Get("Cache-Control") != wantCache || response.Header().Get("X-Content-Type-Options") != "nosniff" {
			t.Fatalf("binary route denial %s: status=%d headers=%v", path, response.Code, response.Header())
		}
	}
	for _, request := range []struct {
		method, path string
		status       int
	}{{http.MethodGet, "/api/v1/plugin/tools", http.StatusUnauthorized}, {http.MethodPost, "/api/v1/dify/retrieval", http.StatusForbidden}} {
		response := httptest.NewRecorder()
		app.ServeHTTP(response, httptest.NewRequest(request.method, request.path, strings.NewReader(`{}`)))
		if response.Code != request.status {
			t.Fatalf("protected route %s: %d", request.path, response.Code)
		}
	}
	health := httptest.NewRecorder()
	app.ServeHTTP(health, httptest.NewRequest(http.MethodGet, "/api/v1/dify/retrieval/health", nil))
	var result map[string]interface{}
	if err := json.Unmarshal(health.Body.Bytes(), &result); err != nil || health.Code != http.StatusOK {
		t.Fatalf("public dependency-free Dify health status=%d result=%v error=%v", health.Code, result, err)
	}
	if routes["GET /api/v1/dify/retrieval"] != "" {
		t.Fatal("retrieval must not run through a GET request")
	}
}
