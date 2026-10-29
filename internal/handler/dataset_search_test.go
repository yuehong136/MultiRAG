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
	for _, body := range []string{`{}`, `{"question":"q"}`, `{"question":"q","dataset_ids":[]}`, `{"question":"q","dataset_ids":[""]}`, `{"question":"q","dataset_ids":[" "]}`, `{"question":"q","dataset_ids":"id"}`, `{"question":"q","dataset_ids":[1]}`, `{"question":"q","kb_id":["id"]}`, `{"question":"q","dataset_ids":["kb"],"tenant_rerank_id":"1"}`, `{"question":"q","dataset_ids":["kb"],"tenant_rerank_id":0}`, `{"question":"q","dataset_ids":["kb"],"vector_similarity_weight":2}`, `{"question":"q","dataset_ids":["kb"],"top_k":-1}`, `{"question":"q","dataset_ids":["kb"],"page":0}`, `{"question":"q","dataset_ids":["kb"],"size":0}`, `{"question":"q","dataset_ids":["kb"],"keywrod":true}`, `{"question":"q","dataset_ids":["kb"],"page":9223372036854775807,"size":2}`} {
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
