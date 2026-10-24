package models

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
)

func TestAzureOpenAIEmbeddingDeploymentProtocol(t *testing.T) {
	name := "embed/deployment?#"
	var requests atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.Method != http.MethodPost || r.URL.EscapedPath() != "/openai/deployments/"+url.PathEscape(name)+"/embeddings" || r.URL.Query().Get("api-version") != azureOpenAIAPIVersion || len(r.URL.Query()) != 1 || r.Header.Get("api-key") != "key" || r.Header.Get("Authorization") != "" {
			t.Error("Azure embedding endpoint/header lost", r.URL)
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if _, exists := body["model"]; exists {
			t.Error("deployment must be selected in the path")
		}
		if body["dimensions"] != float64(2) || body["query"] != nil {
			t.Error("dimensions/query changed", body)
		}
		inputs := body["input"].([]any)
		data := []map[string]any{}
		for i := len(inputs) - 1; i >= 0; i-- {
			data = append(data, map[string]any{"index": i, "embedding": []float64{float64(len(inputs[i].(string))), 0}})
		}
		json.NewEncoder(w).Encode(map[string]any{"data": data})
	}))
	defer srv.Close()
	urls := map[string]string{"region": srv.URL + "/openai"}
	driver, err := NewModelFactory().CreateModelDriver("AZURE-OPENAI", urls, URLSuffix{Embedding: "embeddings"})
	if err != nil || driver.Name() != "azure-openai" {
		t.Fatal(driver, err)
	}
	urls["region"] = "https://wrong.invalid"
	api := &APIConfig{APIKey: stringPtr("key"), Region: stringPtr("region")}
	texts := make([]string, 33)
	for i := range texts {
		texts[i] = strings.Repeat("x", i+1)
	}
	result, err := driver.Embed(&name, texts, api, &EmbeddingConfig{Query: true, Dimension: 2})
	if err != nil || len(result) != 33 || requests.Load() != 2 || *api.Region != "region" {
		t.Fatal(result, err)
	}
	for i, data := range result {
		if data.Index != i || data.Embedding[0] != float64(i+1) || data.Embedding[1] != 0 {
			t.Fatal("indexed embedding lost", data)
		}
	}
	if result, err := driver.Embed(nil, nil, nil, nil); err != nil || result == nil || len(result) != 0 || requests.Load() != 2 {
		t.Fatal("empty input request", result, err)
	}
	if _, err := driver.Embed(&name, []string{"q"}, &APIConfig{APIKey: stringPtr("key"), Region: stringPtr("missing")}, nil); err == nil {
		t.Fatal("unknown region silently used")
	}
}

func TestAzureOpenAIRejectsMalformedEmbeddings(t *testing.T) {
	for _, raw := range []string{
		`{"data":[{"index":0,"embedding":[0,1]},{"index":0,"embedding":[0,1]}]}`,
		`{"data":[{"embedding":[0,1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":null,"embedding":[0,1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":2,"embedding":[0,1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":0,"embedding":[null,1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":0,"embedding":[]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":0,"embedding":[1e400,1]},{"index":1,"embedding":[0,1]}]}`,
		`{"data":[{"index":0,"embedding":[1]},{"index":1,"embedding":[0]}]}`,
		`{"data":[{"index":0,"embedding":[0,1]}]}`, `{}`, `null`, `{"data":null}`, `{} {}`, `{"error":"private-secret"}`,
	} {
		t.Run(raw, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, raw) }))
			defer srv.Close()
			driver := NewAzureOpenAIModel(map[string]string{"default": srv.URL + "/openai"}, URLSuffix{Embedding: "embeddings"})
			if data, err := driver.Embed(stringPtr("deployment"), []string{"a", "b"}, &APIConfig{APIKey: stringPtr("key")}, &EmbeddingConfig{Dimension: 2}); err == nil || data != nil || strings.Contains(err.Error(), "private-secret") {
				t.Fatal(data, err)
			}
		})
	}
}

func TestAzureOpenAIAnnotationsAndFiltering(t *testing.T) {
	text := "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"answer\"}}]}\n"
	finish := "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n"
	annotation := `{"choices":[{"index":0,"content_filter_results":{"hate":{"filtered":false,"severity":"safe"}},"content_filter_offsets":{"check_offset":0,"start_offset":0,"end_offset":6}}]}`
	for _, test := range []struct {
		name, events string
		valid        bool
	}{
		{"prompt-filter", "data: {\"prompt_filter_results\":[{\"prompt_index\":0,\"content_filter_results\":{\"hate\":{\"filtered\":false}}}],\"choices\":[],\"usage\":null}\n" + text + finish + "data: " + annotation + "\ndata: [DONE]\n", true},
		{"prompt-annotations", "data: {\"prompt_annotations\":[{\"prompt_index\":0,\"content_filter_results\":{}}],\"choices\":[],\"usage\":null}\n" + text + "data: " + annotation + "\n" + finish + "data: [DONE]\n", true},
		{"content-filter", text + "data: {\"choices\":[{\"finish_reason\":\"content_filter\"}]}\ndata: [DONE]\n", false},
		{"late-filter", text + finish + "data: {\"choices\":[{\"index\":0,\"content_filter_results\":{\"hate\":{\"filtered\":true}}}]}\ndata: [DONE]\n", false},
		{"null-prompt", "data: {\"prompt_filter_results\":null,\"choices\":[]}\n" + text + finish, false},
		{"bad-prompt", "data: {\"prompt_annotations\":[null],\"choices\":[]}\n" + text + finish, false},
		{"null-filtered", text + "data: {\"choices\":[{\"index\":0,\"content_filter_results\":{\"hate\":{\"filtered\":null}}}]}\n" + finish, false},
		{"missing-offset", text + "data: {\"choices\":[{\"index\":0,\"content_filter_results\":{}}]}\n" + finish, false},
		{"bad-offset", text + "data: {\"choices\":[{\"index\":0,\"content_filter_results\":{},\"content_filter_offsets\":{\"start_offset\":1,\"end_offset\":0}}]}\n" + finish, false},
		{"annotation-only", "data: " + annotation + "\ndata: [DONE]\n", false},
		{"metadata-error", text + "data: {\"prompt_filter_results\":[{\"prompt_index\":0,\"content_filter_results\":{}}],\"choices\":[],\"error\":{\"message\":\"private-secret\"}}\ndata: [DONE]\n", false},
		{"choice-metadata-error", text + "data: {\"choices\":[{\"index\":0,\"content_filter_results\":{},\"content_filter_offsets\":{\"check_offset\":0,\"start_offset\":0,\"end_offset\":1}}],\"error\":\"private-secret\"}\ndata: [DONE]\n", false},
		{"null-choices", "data: {\"prompt_filter_results\":[],\"choices\":null}\n" + text + finish, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprint(w, test.events)
			}))
			defer srv.Close()
			driver := NewAzureOpenAIModel(map[string]string{"default": srv.URL + "/openai"}, URLSuffix{Chat: "chat/completions"})
			var answer string
			err := driver.ChatStreamlyWithMessages("tenant-deployment", []Message{{Role: "user", Content: "hi"}}, &APIConfig{APIKey: stringPtr("key")}, nil, func(c, r *string) error {
				if c != nil {
					answer += *c
				}
				return nil
			})
			if test.valid != (err == nil && answer == "answer[DONE]") || !test.valid && strings.Contains(answer, "[DONE]") || err != nil && strings.Contains(err.Error(), "private-secret") {
				t.Fatal(answer, err)
			}
		})
	}
	for _, extra := range []string{`"finish_reason":"content_filter"`, `"content_filter_results":{"hate":{"filtered":true}}`} {
		t.Run("nonstream/"+extra, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				fmt.Fprint(w, `{"choices":[{"message":{"content":"partial"},`+extra+`}]}`)
			}))
			defer srv.Close()
			driver := NewAzureOpenAIModel(map[string]string{"default": srv.URL + "/openai"}, URLSuffix{Chat: "chat/completions"})
			if result, err := driver.ChatWithMessages("deployment", &APIConfig{APIKey: stringPtr("key")}, []Message{{Role: "user", Content: "hi"}}, nil); err == nil || result != nil {
				t.Fatal("filtered response returned success", result, err)
			}
		})
	}
}

func TestAzureOpenAIRealModelDiscoveryContract(t *testing.T) {
	var obsolete atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/openai/deployments" {
			obsolete.Add(1)
			w.WriteHeader(404)
			return
		}
		if r.URL.Path != "/openai/models" || r.URL.Query().Get("api-version") != azureOpenAIAPIVersion || r.Header.Get("api-key") != "key" {
			t.Error("incorrect official discovery request", r.URL)
		}
		fmt.Fprint(w, `{"object":"list","data":[{"id":"base-model-id","object":"model","status":"succeeded","capabilities":{"chat_completion":true,"inference":true}}]}`)
	}))
	defer srv.Close()
	driver := NewAzureOpenAIModel(map[string]string{"default": srv.URL + "/openai"}, URLSuffix{Models: "models"})
	names, err := driver.ListModels(&APIConfig{APIKey: stringPtr("key")})
	if err != nil || len(names) != 1 || names[0] != "base-model-id" || obsolete.Load() != 0 {
		t.Fatal(names, err)
	}
	if err := driver.CheckConnection(&APIConfig{APIKey: stringPtr("key")}); err != nil {
		t.Fatal(err)
	}
	old := NewAzureOpenAIModel(map[string]string{"default": srv.URL + "/openai"}, URLSuffix{Models: "deployments"})
	if _, err := old.ListModels(&APIConfig{APIKey: stringPtr("key")}); err == nil || obsolete.Load() != 1 {
		t.Fatal("obsolete endpoint silently succeeded", err)
	}
}
