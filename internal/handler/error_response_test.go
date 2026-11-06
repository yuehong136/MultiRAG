package handler

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
	"multirag/internal/common"
)

func TestJSONInternalErrorRedactsResponseAndLogsCause(t *testing.T) {
	core, logs := observer.New(zap.WarnLevel)
	previous := common.Logger
	common.Logger = zap.New(core)
	t.Cleanup(func() { common.Logger = previous })
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(http.MethodGet, "/api/v1/files?token=query-private", nil)
	raw := "controlled backend failure host=private-db password=private-marker"
	jsonInternalError(c, errors.New(raw))
	var body struct {
		Code    common.ErrorCode
		Message string
		Data    any
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if w.Code != http.StatusOK || body.Code != common.CodeServerError || body.Message != common.CodeServerError.Message() || body.Data != nil || strings.Contains(w.Body.String(), "private") {
		t.Fatalf("unexpected internal error response: %d %s", w.Code, w.Body)
	}
	entries := logs.All()
	if len(entries) != 1 {
		t.Fatal("internal error was not logged exactly once")
	}
	fields := entries[0].ContextMap()
	if len(fields) != 3 || fields["error"] != raw || fields["method"] != http.MethodGet || fields["path"] != "/api/v1/files" {
		t.Fatalf("missing raw cause or unexpected query in log: %v", fields)
	}
}
