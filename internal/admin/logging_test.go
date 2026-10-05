package admin

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
)

func TestAdminLogLevelHTTP(t *testing.T) {
	defer common.SetLevel(common.GetLevel())
	handler := &Handler{}
	router := gin.New()
	router.GET("/level", handler.GetLogLevel)
	router.PUT("/level", handler.SetLogLevel)
	captured := common.Logger.With()
	for _, test := range []struct {
		body   string
		status int
		level  string
	}{
		{`{"level":"debug"}`, http.StatusOK, "debug"},
		{`{"level":"invalid"}`, http.StatusBadRequest, "debug"},
		{`{}`, http.StatusBadRequest, "debug"},
		{`{"level":"error"}`, http.StatusOK, "error"},
	} {
		response := httptest.NewRecorder()
		router.ServeHTTP(response, httptest.NewRequest(http.MethodPut, "/level", strings.NewReader(test.body)))
		if response.Code != test.status || common.GetLevel() != test.level {
			t.Fatalf("%s: %d %s", test.body, response.Code, response.Body.String())
		}
		response = httptest.NewRecorder()
		router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/level", nil))
		var result struct {
			Code int
			Data struct{ Level string }
		}
		if err := json.Unmarshal(response.Body.Bytes(), &result); err != nil {
			t.Fatal(err)
		}
		if result.Code != 0 || result.Data.Level != test.level {
			t.Fatalf("readback: %+v", result)
		}
		if captured.Core().Enabled(-1) != (test.level == "debug") {
			t.Fatal("captured logger did not update")
		}
	}
}
