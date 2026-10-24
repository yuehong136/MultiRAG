package handler

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/entity"
)

func TestDatasetTagRequestValidationPrecedesSQLAndStorage(t *testing.T) {
	app := gin.New()
	app.Use(func(c *gin.Context) { c.Set("user", &entity.User{ID: "owner"}) })
	app.DELETE("/datasets/:dataset_id/tags", (&DatasetsHandler{}).RemoveTags)
	for _, body := range []string{`{}`, `[]`, `null`, `{"tags":null}`, `{"tags":[]}`, `{"tags":[""]}`, `{"tags":[" "]}`, `{"tags":["one","one"]}`, `{"tags":["one"],"tags":["two"]}`, `{"tags":[1]}`, `{"tags":["one"],"unknown":true}`, `{"tags":["one"]} {}`} {
		response := httptest.NewRecorder()
		app.ServeHTTP(response, httptest.NewRequest(http.MethodDelete, "/datasets/owned/tags", strings.NewReader(body)))
		var result struct{ Code common.ErrorCode }
		if err := json.Unmarshal(response.Body.Bytes(), &result); err != nil || result.Code != common.CodeDataError {
			t.Fatal(body, response.Body.String(), err)
		}
	}
	for _, suffix := range []string{"", "garbage"} {
		body := `{"tags":["one"]}` + strings.Repeat(" ", 1024*1024) + suffix
		request := httptest.NewRequest(http.MethodDelete, "/datasets/owned/tags", strings.NewReader(body))
		request.ContentLength = -1
		request.TransferEncoding = []string{"chunked"}
		response := httptest.NewRecorder()
		app.ServeHTTP(response, request)
		var result struct{ Code common.ErrorCode }
		if err := json.Unmarshal(response.Body.Bytes(), &result); err != nil || result.Code != common.CodeDataError {
			t.Fatal("oversize unknown-length request reached SQL/storage", response.Body.String(), err)
		}
	}
}
