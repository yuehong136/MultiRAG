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
	var found bool
	for _, route := range engine.Routes() {
		if route.Method == http.MethodPost && route.Path == "/api/v1/chat/completions" {
			found = true
		}
		if route.Method == http.MethodPost && route.Path == "/api/v1/providers/:provider_name/instances/:instance_name/models" {
			t.Fatal("old model chat route still registered")
		}
	}
	if !found {
		t.Fatal("new model chat route missing")
	}
	w := httptest.NewRecorder()
	engine.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/api/v1/chat/completions", strings.NewReader(`{}`)))
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated status = %d", w.Code)
	}
}
