package models

import (
	"encoding/json"
	"fmt"
	"net/http"
	"reflect"
	"testing"
)

func TestAstraflowCapabilitiesHTTP(t *testing.T) {
	fixture := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer fixture" {
			t.Error("lost credentials")
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		switch r.URL.Path {
		case "/office/embeddings":
			if body["model"] != "text-embedding-3-large" || body["dimensions"] != float64(2) {
				t.Error(body)
			}
			fmt.Fprint(w, `{"data":[{"index":1,"embedding":[2,3]},{"index":0,"embedding":[0,1]}]}`)
		case "/office/rerank":
			if body["model"] != "bge-reranker-v2-m3" || body["top_n"] != float64(2) {
				t.Error(body)
			}
			fmt.Fprint(w, `{"results":[{"index":1,"relevance_score":0.7},{"index":0,"relevance_score":0}]}`)
		case "/office/audio/speech":
			if body["model"] != "IndexTeam/IndexTTS-2" || body["input"] != "hello" || body["format"] != "wav" || body["response_format"] != nil {
				t.Error(body)
			}
			w.Header().Set("Content-Type", "audio/wav")
			w.Write([]byte{0, 255, 1, 2})
		default:
			t.Error(r.URL.Path)
			w.WriteHeader(404)
		}
	}))
	defer fixture.Close()
	urls := map[string]string{"default": "http://unused.invalid", "office": fixture.URL + "/office/"}
	driver := NewAstraflowModel(urls, URLSuffix{Embedding: "embeddings", Rerank: "rerank", TTS: "audio/speech"})
	urls["office"] = "http://mutated.invalid"
	api := &APIConfig{APIKey: stringPtr("fixture"), Region: stringPtr("office")}
	name := "text-embedding-3-large"
	bound := NewEmbeddingModel(driver, &name, api)
	bound.EmbeddingConfig = &EmbeddingConfig{Dimension: 2}
	vectors, err := bound.Embed([]string{"one", "two"})
	if err != nil || !reflect.DeepEqual(vectors, []EmbeddingData{{Index: 0, Embedding: []float64{0, 1}}, {Index: 1, Embedding: []float64{2, 3}}}) {
		t.Fatal(vectors, err)
	}
	name = "bge-reranker-v2-m3"
	scores, err := NewRerankModel(driver, &name, api).Rerank("query", []string{"one", "two"})
	if err != nil || !reflect.DeepEqual(scores, []float64{0, 0.7}) {
		t.Fatal(scores, err)
	}
	name = "IndexTeam/IndexTTS-2"
	speech, err := driver.AudioSpeech(&name, "hello", api, &TTSConfig{Format: "wav"})
	if err != nil || !reflect.DeepEqual(speech.Audio, []byte{0, 255, 1, 2}) {
		t.Fatal(speech, err)
	}
}
