package handler

import (
	"encoding/json"
	"github.com/gin-gonic/gin"
	"multirag/internal/entity"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestDatasetSearchValidatesBeforeRetrieval(t *testing.T) {
	app := gin.New()
	app.Use(func(c *gin.Context) { c.Set("user", &entity.User{ID: "owned-user"}) })
	app.POST("/api/v1/datasets/search", (&ChunkHandler{}).SearchDatasets)
	for _, body := range []string{`{}`, `{"question":"q"}`, `{"question":"q","dataset_ids":[]}`, `{"question":"q","dataset_ids":[""]}`, `{"question":"q","dataset_ids":[" "]}`, `{"question":"q","dataset_ids":"id"}`, `{"question":"q","dataset_ids":[1]}`, `{"question":"q","kb_id":["id"]}`} {
		response := httptest.NewRecorder()
		app.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/api/v1/datasets/search", strings.NewReader(body)))
		var result struct{ Code int }
		if err := json.Unmarshal(response.Body.Bytes(), &result); err != nil {
			t.Fatal(err)
		}
		if response.Code != http.StatusBadRequest || result.Code != 400 {
			t.Fatalf("invalid request accepted: %s: %d %s", body, response.Code, response.Body.String())
		}
	}
}
