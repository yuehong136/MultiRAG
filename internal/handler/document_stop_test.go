package handler

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"multirag/internal/common"
	"multirag/internal/entity"

	"github.com/gin-gonic/gin"
)

func TestDocumentStopRejectsBeforeService(t *testing.T) {
	for _, tc := range []struct {
		name, body, dataset string
		authenticated       bool
		code                common.ErrorCode
	}{
		{"authentication", `{"document_ids":["a"]}`, "kb", false, common.CodeUnauthorized},
		{"dataset", `{"document_ids":["a"]}`, "", true, common.CodeBadRequest},
		{"nonobject", `[]`, "kb", true, common.CodeBadRequest},
		{"missing", `{}`, "kb", true, common.CodeBadRequest},
		{"null", `{"document_ids":null}`, "kb", true, common.CodeBadRequest},
		{"empty", `{"document_ids":[]}`, "kb", true, common.CodeBadRequest},
		{"numeric", `{"document_ids":[1]}`, "kb", true, common.CodeBadRequest},
		{"string", `{"document_ids":"a"}`, "kb", true, common.CodeBadRequest},
		{"duplicate", `{"document_ids":["a"],"document_ids":["b"]}`, "kb", true, common.CodeBadRequest},
		{"trailing", `{"document_ids":["a"]} {}`, "kb", true, common.CodeBadRequest},
	} {
		t.Run(tc.name, func(t *testing.T) {
			w := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(w)
			c.Request = httptest.NewRequest("POST", "/api/v1/datasets/kb/documents/stop", strings.NewReader(tc.body))
			c.Params = gin.Params{{Key: "dataset_id", Value: tc.dataset}}
			if tc.authenticated {
				c.Set("user", &entity.User{ID: "owner"})
			}
			(&DocumentHandler{}).StopParseDocuments(c)
			var result struct{ Code common.ErrorCode }
			if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil || result.Code != tc.code {
				t.Fatalf("response %s; wanted %d", w.Body.String(), tc.code)
			}
		})
	}
}
