package handler

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
)

func TestSystemLogLevelHTTP(t *testing.T) {
	defer common.SetLevel(common.GetLevel())
	if err := common.Init("info", ""); err != nil {
		t.Fatal(err)
	}
	h := &SystemHandler{}
	app := gin.New()
	app.GET("/level", h.GetLogLevel)
	app.PUT("/level", h.SetLogLevel)
	captured := common.Logger.With()
	for _, test := range []struct {
		body, level string
		code        int
	}{{`{"pkg_name":"root","level":"debug"}`, "debug", 0}, {`{"pkg_name":"root","level":"invalid"}`, "debug", 102}, {`{}`, "debug", 102}, {`{"pkg_name":"root","level":"error"}`, "error", 0}} {
		w := httptest.NewRecorder()
		app.ServeHTTP(w, httptest.NewRequest(http.MethodPut, "/level", strings.NewReader(test.body)))
		var changed struct{ Code int }
		if err := json.Unmarshal(w.Body.Bytes(), &changed); err != nil || w.Code != 200 || changed.Code != test.code || common.GetLevel() != test.level {
			t.Fatal("canonical package log setter", test, w.Body.String(), err)
		}
		w = httptest.NewRecorder()
		app.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/level", nil))
		var result struct {
			Code int
			Data map[string]string
		}
		if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil || result.Code != 0 || strings.ToLower(result.Data["root"]) != test.level {
			t.Fatal("actual package log readback", w.Body.String(), err)
		}
		if captured.Core().Enabled(-1) != (test.level == "debug") {
			t.Fatal("captured logger level did not update")
		}
	}
}
