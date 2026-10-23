package models

import (
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestZhipuEmbeddingEmptyRegionAndNilConfigHTTP(t *testing.T) {
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/embeddings" {
			t.Error(r.URL.Path)
		}
		io.WriteString(w, `{"data":[{"index":0,"embedding":[0,1]}]}`)
	}))
	defer api.Close()
	driver := NewZhipuAIModel(map[string]string{"default": api.URL}, URLSuffix{Embedding: "embeddings"})
	name, key, region := "embedding", "controlled", ""
	result, err := driver.Embed(&name, []string{"query"}, &APIConfig{APIKey: &key, Region: &region}, nil)
	if err != nil || len(result) != 1 || len(result[0].Embedding) != 2 {
		t.Fatal(result, err)
	}
}
