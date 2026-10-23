package router

import (
	"github.com/gin-gonic/gin"
	"multirag/internal/handler"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestCLIAPIRouteScope(t *testing.T) {
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	r := &Router{authHandler: handler.NewAuthHandler()}
	r.Setup(app)
	routes := map[string]string{}
	for _, route := range app.Routes() {
		routes[route.Method+" "+route.Path] = route.Handler
	}
	for _, key := range []string{"GET /api/v1/system/ping", "GET /api/v1/system/config", "GET /api/v1/system/version", "POST /api/v1/auth/login", "POST /api/v1/users", "GET /api/v1/auth/login/channels"} {
		if routes[key] == "" {
			t.Fatal("missing public contract", key)
		}
	}
	if !strings.HasSuffix(routes["POST /api/v1/auth/login"], ".Login-fm") {
		t.Fatal("REST login must retain current username/JWT policy")
	}
	for _, key := range []string{"POST /v1/user/login", "POST /v1/user/register", "GET /v1/system/ping", "POST /api/v1/chunk/retrieval_test"} {
		if routes[key] != "" {
			t.Fatal("obsolete public route", key)
		}
	}
	for _, entry := range [][2]string{{"GET", "/api/v1/system/configs"}, {"POST", "/api/v1/auth/logout"}, {"POST", "/api/v1/tenant/doc_engine_metadata_table"}, {"POST", "/api/v1/kb/doc_engine_table"}, {"POST", "/api/v1/datasets/search"}, {"GET", "/api/v1/datasets/id"}, {"POST", "/api/v1/document/set_meta"}} {
		w := httptest.NewRecorder()
		app.ServeHTTP(w, httptest.NewRequest(entry[0], entry[1], strings.NewReader(`{}`)))
		if w.Code != http.StatusUnauthorized {
			t.Fatalf("%s %s status %d", entry[0], entry[1], w.Code)
		}
	}
}
