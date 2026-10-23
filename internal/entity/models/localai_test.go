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
	"strings"
	"testing"
	"time"
)

func TestLocalAIChatAndStreaming(t *testing.T) {
	for _, alias := range []string{"reasoning_content", "reasoning", "thinking"} {
		t.Run(alias, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "" || r.URL.Path != "/v1/chat/completions" {
					t.Error("optional auth/path")
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				messages := body["messages"].([]any)
				if len(messages) != 3 || messages[0].(map[string]any)["role"] != "system" || body["reasoning_effort"] != "high" || body["enable_thinking"] != true || body["max_tokens"] != float64(42) {
					t.Error("history/config lost", body)
				}
				if body["stream"] == true {
					fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"%s\":\"推理🙂\"}}]}\n\ndata: {\"choices\":[{\"delta\":{\"content\":\"答案🙂\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n", alias)
				} else {
					fmt.Fprintf(w, `{"choices":[{"message":{"content":"答案🙂",%q:"推理🙂"}}]}`, alias)
				}
			}))
			defer srv.Close()
			driver := NewLocalAIModel(map[string]string{"default": srv.URL + "/v1/"}, URLSuffix{Chat: "chat/completions"})
			effort := "high"
			thinking := true
			max := 42
			config := &ChatConfig{Effort: &effort, Thinking: &thinking, MaxTokens: &max}
			messages := []Message{{Role: "system", Content: "rules"}, {Role: "assistant", Content: "history"}, {Role: "user", Content: "x"}}
			result, err := driver.ChatWithMessages("local", nil, messages, config)
			if err != nil || *result.Answer != "答案🙂" || result.ReasoningContent == nil || *result.ReasoningContent != "推理🙂" {
				t.Fatalf("%v %v", result, err)
			}
			var got []string
			err = driver.ChatStreamlyWithMessages("local", messages, nil, config, func(c, r *string) error {
				if c != nil {
					got = append(got, *c)
				}
				if r != nil {
					got = append(got, *r)
				}
				return nil
			})
			if err != nil || !reflect.DeepEqual(got, []string{"推理🙂", "答案🙂", "[DONE]"}) {
				t.Fatalf("stream %v %v", got, err)
			}
		})
	}
}
func TestLocalAIStreamFailureLifecycle(t *testing.T) {
	for _, test := range []struct {
		name, body string
		wantErr    bool
	}{
		{"done", "data: {\"choices\":[{\"delta\":{\"content\":\"ok\"}}]}\n\ndata: [DONE]\n\n", false},
		{"truncated", "data: {\"choices\":[{\"delta\":{\"content\":\"ok\"}}]}\n", true},
		{"malformed", "data: invalid\n", true},
		{"provider-error", "data: {\"error\":\"failed\"}\n", true},
		{"empty", "data: [DONE]\n", true},
	} {
		t.Run(test.name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, test.body) }))
			defer srv.Close()
			d := NewLocalAIModel(map[string]string{"default": srv.URL}, URLSuffix{Chat: "chat"})
			err := d.ChatStreamlyWithMessages("model", []Message{{Role: "user", Content: "x"}}, nil, nil, func(*string, *string) error { return nil })
			if (err != nil) != test.wantErr {
				t.Fatalf("%v", err)
			}
		})
	}
	t.Run("idle-and-cancel", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			io.WriteString(w, "data: {\"choices\":[{\"delta\":{\"content\":\"ok\"}}]}\n\n")
			w.(http.Flusher).Flush()
			<-r.Context().Done()
		}))
		defer srv.Close()
		d := NewLocalAIModel(map[string]string{"default": srv.URL}, URLSuffix{Chat: "chat"})
		d.streamIdleTimeout = 30 * time.Millisecond
		err := d.ChatStreamlyWithMessages("m", []Message{{Role: "user", Content: "x"}}, nil, nil, func(*string, *string) error { return nil })
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatal("idle", err)
		}
		ctx, cancel := context.WithCancel(context.Background())
		err = d.ChatStreamlyWithMessages("m", []Message{{Role: "user", Content: "x"}}, &APIConfig{Context: ctx}, nil, func(*string, *string) error { cancel(); return nil })
		if !errors.Is(err, context.Canceled) {
			t.Fatal("caller cancel", err)
		}
		sentinel := errors.New("sender failed")
		err = d.ChatStreamlyWithMessages("m", []Message{{Role: "user", Content: "x"}}, nil, nil, func(*string, *string) error { return sentinel })
		if !errors.Is(err, sentinel) {
			t.Fatal("sender", err)
		}
	})
}
func TestLocalAIEmbeddingRerankDiscovery(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer fixture" {
			t.Error("auth")
		}
		if r.URL.Path == "/models" {
			io.WriteString(w, `{"data":[{"id":"local","owned_by":"ignored"}]}`)
			return
		}
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		switch r.URL.Path {
		case "/embeddings":
			if body["dimensions"] != float64(2) {
				t.Error("dimension")
			}
			if _, ok := body["query"]; ok {
				t.Error("local metadata leaked")
			}
			io.WriteString(w, `{"data":[{"index":1,"embedding":[2,3]},{"index":0,"embedding":[0,1]}]}`)
		case "/rerank":
			if body["top_n"] != float64(2) {
				t.Error("top_n")
			}
			io.WriteString(w, `{"results":[{"index":0,"relevance_score":0},{"index":1,"relevance_score":1}]}`)
		default:
			t.Error("unexpected endpoint", r.URL.Path)
		}
	}))
	defer srv.Close()
	d := NewLocalAIModel(map[string]string{"default": srv.URL}, URLSuffix{Models: "models", Embedding: "embeddings", Rerank: "rerank"})
	key, name := "fixture", "local"
	api := &APIConfig{APIKey: &key}
	vectors, err := d.Embed(&name, []string{"a", "b"}, api, &EmbeddingConfig{Query: true, Dimension: 2})
	if err != nil || len(vectors) != 2 || vectors[0].Embedding[0] != 0 {
		t.Fatalf("embed %v %v", vectors, err)
	}
	scores, err := d.Rerank(&name, "q", []string{"a", "b"}, api, nil)
	if err != nil || len(scores.Data) != 2 || scores.Data[1].RelevanceScore != 0 {
		t.Fatalf("rerank %v %v", scores, err)
	}
	if names, err := d.ListModels(api); err != nil || !reflect.DeepEqual(names, []string{"local"}) {
		t.Fatal(names, err)
	}
	if err := d.CheckConnection(api); err != nil {
		t.Fatal(err)
	}
	if _, err := d.Rerank(&name, "q", []string{"x"}, nil, &RerankConfig{TopN: -1}); err == nil {
		t.Fatal("negative top accepted")
	}
	if _, ok := any(d).(OCRDriver); ok {
		t.Fatal("LocalAI declares OCR")
	}
	if _, ok := any(d).(OCRURLDriver); ok {
		t.Fatal("LocalAI declares OCR URL")
	}
	if _, err := d.Balance(nil); !errors.Is(err, ErrUnsupportedCapability) {
		t.Fatal(err)
	}
	if _, err := d.Embed(nil, nil, nil, nil); err != nil {
		t.Fatal("empty embeds", err)
	}
	if _, err := d.Rerank(nil, "", nil, nil, nil); err != nil {
		t.Fatal("empty rerank", err)
	}
	missing := NewLocalAIModel(nil, URLSuffix{Models: "models"})
	if _, err := missing.ListModels(nil); err == nil {
		t.Fatal("missing tenant URL accepted")
	}
	for _, bad := range []string{"", "unknown"} {
		region := bad
		if bad != "" {
			if _, err := d.ListModels(&APIConfig{Region: &region}); err == nil {
				t.Fatal("unknown region")
			}
		}
	}
}

type customProviderTransport struct{}

func (customProviderTransport) RoundTrip(*http.Request) (*http.Response, error) {
	return nil, fmt.Errorf("custom transport")
}
func TestLocalAIConstructorCustomDefaultTransport(t *testing.T) {
	previous := http.DefaultTransport
	http.DefaultTransport = customProviderTransport{}
	defer func() { http.DefaultTransport = previous }()
	d := NewLocalAIModel(map[string]string{"default": "http://127.0.0.1:1"}, URLSuffix{})
	if d.Name() != "LocalAI" || d.httpClient.Timeout != 0 {
		t.Fatal("constructor")
	}
	tr := d.httpClient.Transport.(*http.Transport)
	if tr.TLSHandshakeTimeout <= 0 || tr.ResponseHeaderTimeout <= 0 {
		t.Fatal("transport deadline")
	}
	tr.CloseIdleConnections()
}

func TestLocalAIStrictInferenceResults(t *testing.T) {
	for _, body := range []string{`{"data":[{"embedding":[1]}]}`, `{"data":[{"index":null,"embedding":[1]}]}`, `{"data":[{"index":0,"embedding":[null]}]}`, `{"data":[{"index":0,"embedding":[1,2]}]}`, `{"error":"failed"}`, `{"data":[]}`} {
		t.Run(body, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, body) }))
			defer srv.Close()
			d := NewLocalAIModel(map[string]string{"default": srv.URL}, URLSuffix{Embedding: "embeddings"})
			name := "m"
			if _, err := d.Embed(&name, []string{"x"}, nil, &EmbeddingConfig{Dimension: 1}); err == nil {
				t.Fatal("bad embedding accepted")
			}
		})
	}
	for _, body := range []string{`{"results":[{"index":0}]}`, `{"results":[{"index":null,"relevance_score":0}]}`, `{"results":[{"index":0,"relevance_score":null}]}`, `{"results":[{"index":2,"relevance_score":0}]}`, `{"results":[{"index":0,"relevance_score":0},{"index":0,"relevance_score":1}]}`, `{"results":[]}`, `{"results":[{"index":0,"relevance_score":1e999}]}`} {
		t.Run(strings.ReplaceAll(body, "/", ""), func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, body) }))
			defer srv.Close()
			d := NewLocalAIModel(map[string]string{"default": srv.URL}, URLSuffix{Rerank: "rerank"})
			name := "m"
			if _, err := d.Rerank(&name, "q", []string{"x"}, nil, nil); err == nil {
				t.Fatal("bad rerank accepted")
			}
		})
	}
}
