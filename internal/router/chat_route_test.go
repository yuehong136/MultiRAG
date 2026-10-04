package router

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/handler"
)

func TestModelChatRouteRequiresAuthentication(t *testing.T) {
	gin.SetMode(gin.ReleaseMode)
	engine := gin.New()
	r := &Router{authHandler: handler.NewAuthHandler()}
	r.Setup(engine)
	var found, foundDeclaration bool
	for _, route := range engine.Routes() {
		if route.Method == http.MethodPost && route.Path == "/api/v1/chat/completions" {
			found = true
		}
		if route.Method == http.MethodPost && route.Path == "/api/v1/providers/:provider_name/instances/:instance_name/models" {
			foundDeclaration = true
			if !strings.HasSuffix(route.Handler, ".AddCustomModel-fm") {
				t.Fatalf("model declaration route bound to %s", route.Handler)
			}
		}
	}
	if !found {
		t.Fatal("new model chat route missing")
	}
	if !foundDeclaration {
		t.Fatal("model declaration route missing")
	}
	for _, path := range []string{"/api/v1/chat/completions", "/api/v1/providers/vllm/instances/local/models"} {
		w := httptest.NewRecorder()
		engine.ServeHTTP(w, httptest.NewRequest(http.MethodPost, path, strings.NewReader(`{}`)))
		if w.Code != http.StatusUnauthorized {
			t.Fatalf("unauthenticated %s status = %d", path, w.Code)
		}
	}
}
