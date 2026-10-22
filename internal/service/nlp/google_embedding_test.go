package nlp

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"

	"multirag/internal/entity/models"
)

func TestGoogleRetrievalUsesQueryTaskWithoutMutatingBoundModel(t *testing.T) {
	for _, config := range []*models.EmbeddingConfig{nil, {Dimension: 2}} {
		t.Run(fmt.Sprintf("%+v", config), func(t *testing.T) {
			var tasks []string
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/v1beta/models/text-embedding-004:batchEmbedContents" || r.Header.Get("x-goog-api-key") != "fixture" {
					t.Error("Google SDK binding lost")
				}
				var body struct {
					Requests []struct {
						TaskType             string
						OutputDimensionality *int
					}
				}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil || len(body.Requests) != 1 {
					t.Error("SDK payload", err)
					return
				}
				request := body.Requests[0]
				tasks = append(tasks, request.TaskType)
				if config == nil {
					if request.OutputDimensionality != nil {
						t.Error("default dimension changed")
					}
				} else if request.OutputDimensionality == nil || *request.OutputDimensionality != config.Dimension {
					t.Error("dimension lost")
				}
				fmt.Fprint(w, `{"embeddings":[{"values":[0,1]}]}`)
			}))
			defer server.Close()
			name, key := "text-embedding-004", "fixture"
			driver := models.NewGoogleModel(map[string]string{"default": server.URL}, models.URLSuffix{})
			model := models.NewEmbeddingModel(driver, &name, &models.APIConfig{APIKey: &key})
			model.EmbeddingConfig = config
			if _, err := embeddingVectorsForTest(model.Embed([]string{"stored document"})); err != nil {
				t.Fatal(err)
			}
			expression, err := NewRetrievalService(nil).GetVector("search question", model, 7, 0.4)
			if err != nil {
				t.Fatal(err)
			}
			if expression.VectorColumnName != "q_2_vec" || expression.TopN != 7 || !reflect.DeepEqual(expression.EmbeddingData, []float64{0, 1}) {
				t.Fatal(expression)
			}
			if _, err := embeddingVectorsForTest(model.Embed([]string{"another document"})); err != nil {
				t.Fatal(err)
			}
			if model.EmbeddingConfig != config || (config != nil && config.Query) || !reflect.DeepEqual(tasks, []string{"RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY", "RETRIEVAL_DOCUMENT"}) {
				t.Fatalf("query config leaked: %v", tasks)
			}
		})
	}
}
