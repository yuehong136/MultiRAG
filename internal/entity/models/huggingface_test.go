package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

func TestHuggingFaceChatHTTP(t *testing.T) {
	testHostedChatHTTP(t, "HuggingFace", func(t *testing.T, body map[string]any) {
		if !reflect.DeepEqual(body["thinking"], map[string]any{"type": "disabled"}) {
			t.Fatal(body)
		}
		if body["stream"] == true && body["do_sample"] != false {
			t.Fatal(body)
		}
	})
}
func TestHuggingFaceFailuresHTTP(t *testing.T) { testHostedChatFailures(t, "HuggingFace", true) }
func TestHuggingFaceCancelHTTP(t *testing.T)   { testHostedCancellation(t, "HuggingFace", true) }
func TestHuggingFaceEmbeddingHTTP(t *testing.T) {
	for _, body := range []string{`[[1,2],[3,4]]`, `[[0,2],[3,0]]`, `[[null,2],[3,4]]`, `[[1,2],[3,4]] {}`, `[]`, `[[1]]`, `[[1,2],[]]`, `[[1,2],[3]]`, `[[1,2],[1e999,2]]`, `{"error":"private"}`} {
		t.Run(body, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/hf-inference/models/org/model" || r.Header.Get("Authorization") != "Bearer fixture" {
					t.Error(r.URL.Path)
				}
				var request map[string]any
				if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
					t.Error(err)
				}
				if !reflect.DeepEqual(request["inputs"], []any{"a", "b"}) {
					t.Error(request)
				}
				fmt.Fprint(w, body)
			}))
			defer server.Close()
			urls := map[string]string{"default": server.URL + "/v1/"}
			driver := NewHuggingFaceModel(urls, URLSuffix{Embedding: "hf-inference/models"})
			urls["default"] = "http://invalid"
			key, name := "fixture", "org/model"
			vectors, err := embeddingVectorsForTest(driver.Embed(&name, []string{"a", "b"}, &APIConfig{APIKey: &key}, nil))
			if (err == nil) != (body == `[[1,2],[3,4]]` || body == `[[0,2],[3,0]]`) {
				t.Fatal(vectors, err)
			}
		})
	}
}
func TestHuggingFaceDiscoveryHTTP(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/models" || r.Method != "GET" {
			t.Error(r.URL)
		}
		fmt.Fprint(w, `{"data":[{"id":"org/m"}]}`)
	}))
	defer server.Close()
	d := NewHuggingFaceModel(map[string]string{"default": server.URL + "/v1"}, URLSuffix{Models: "models"})
	key := "fixture"
	api := &APIConfig{APIKey: &key}
	names, err := d.ListModels(api)
	if err != nil || !reflect.DeepEqual(names, []string{"org/m"}) {
		t.Fatal(names, err)
	}
	if err := d.CheckConnection(api); err != nil {
		t.Fatal(err)
	}
}
func TestHuggingFaceEmbeddingCancelHTTP(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		r.Body.Close()
		cancel()
		<-r.Context().Done()
	}))
	defer server.Close()
	d := NewHuggingFaceModel(map[string]string{"default": server.URL}, URLSuffix{Embedding: "infer"})
	key, name := "fixture", "org/m"
	_, err := embeddingVectorsForTest(d.Embed(&name, []string{"a"}, &APIConfig{APIKey: &key, Context: ctx}, nil))
	if !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if v, err := embeddingVectorsForTest(d.Embed(nil, nil, nil, nil)); err != nil || v == nil || len(v) != 0 {
		t.Fatal(v, err)
	}
	for _, api := range []*APIConfig{nil, {}} {
		if _, err := embeddingVectorsForTest(d.Embed(&name, []string{"a"}, api, nil)); err == nil {
			t.Fatal("missing key accepted")
		}
	}
}

func TestHuggingFaceConfiguredSuffixAndRegionHTTP(t *testing.T) {
	var paths []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		switch r.URL.Path {
		case "/tenant/v1/custom-chat":
			var body map[string]any
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Error(err)
			}
			if body["stream"] == true {
				fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"ok\"}}]}\n\ndata: [DONE]\n\n")
			} else {
				fmt.Fprint(w, `{"choices":[{"message":{"content":"ok"}}]}`)
			}
		case "/tenant/custom-infer/org/m":
			fmt.Fprint(w, `[[0,1]]`)
		default:
			t.Error(r.URL.Path)
			w.WriteHeader(404)
		}
	}))
	defer server.Close()
	driver := NewHuggingFaceModel(map[string]string{"default": "http://unused.invalid", "office": server.URL + "/tenant/v1/"}, URLSuffix{Chat: "custom-chat", Embedding: "custom-infer"})
	api := &APIConfig{APIKey: stringPtr("fixture"), Region: stringPtr("office")}
	messages := []Message{{Role: "user", Content: "q"}}
	response, err := driver.ChatWithMessages("m", api, messages, nil)
	if err != nil || response == nil || *response.Answer != "ok" {
		t.Fatal(response, err)
	}
	if err := driver.ChatStreamlyWithMessages("m", messages, api, nil, func(c, r *string) error { return nil }); err != nil {
		t.Fatal(err)
	}
	vectors, err := embeddingVectorsForTest(driver.Embed(stringPtr("org/m"), []string{"a"}, api, nil))
	if err != nil || !reflect.DeepEqual(vectors, [][]float64{{0, 1}}) {
		t.Fatal(vectors, err)
	}
	if !reflect.DeepEqual(paths, []string{"/tenant/v1/custom-chat", "/tenant/v1/custom-chat", "/tenant/custom-infer/org/m"}) {
		t.Fatal(paths)
	}
	invalid := &APIConfig{APIKey: stringPtr("fixture"), Region: stringPtr("unknown")}
	if _, err := driver.Embed(stringPtr("org/m"), []string{"a"}, invalid, nil); err == nil {
		t.Fatal("unknown region fell back")
	}
	if len(paths) != 3 || api.Context != nil || driver.httpClient.Transport != openAIChatTransport {
		t.Fatal("caller/pool changed")
	}
}
