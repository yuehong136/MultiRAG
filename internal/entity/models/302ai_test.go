package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func newAI302ContractModel(base string) *AI302Model {
	return NewAI302Model(map[string]string{"default": base + "/v1/"}, URLSuffix{Chat: " /chat/ ", Models: " /models/ ", Embedding: " /embed/ ", Rerank: " /rerank/ ", ASR: " /asr/ ", OCR: " /ocr/ ", DocumentParse: " /parse/ ", Task: " /task/ "})
}

func TestAI302NormalizedRequests(t *testing.T) {
	var calls atomic.Int32
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("Authorization") != "Bearer key" {
			t.Error("credential normalization", r.Header)
		}
		if r.Method == http.MethodGet {
			if r.ContentLength != 0 {
				t.Error("GET has body")
			}
			if r.URL.Path == "/v1/models" {
				if r.Header.Get("Accept") != "application/json" {
					t.Error("inventory accept")
				}
				fmt.Fprint(w, `{"data":[]}`)
			} else if r.URL.Path == "/v1/task/job-1" {
				fmt.Fprint(w, `{"code":0,"data":{"task_id":"job-1","state":"running"}}`)
			} else {
				t.Error("GET path", r.URL.Path)
			}
			return
		}
		if r.URL.Path == "/v1/asr" {
			if r.Header.Get("Accept") != "application/json" {
				t.Error("ASR accept")
			}
			if err := r.ParseMultipartForm(1024); err != nil {
				t.Error(err)
				return
			}
			defer r.MultipartForm.RemoveAll()
			file, header, err := r.FormFile("file")
			if err != nil {
				t.Error(err)
				return
			}
			defer file.Close()
			audio, _ := io.ReadAll(file)
			if string(audio) != "audio" || header.Filename != "audio.wav" || r.FormValue("model") != "model" || r.FormValue("temperature") != "0" {
				t.Error("multipart", r.MultipartForm)
			}
			fmt.Fprint(w, `{"text":""}`)
			return
		}
		if r.Header.Get("Content-Type") != "application/json" {
			t.Error("JSON content type")
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		if r.URL.Path != "/v1/parse" && body["model"] != "model" {
			t.Error("model normalization", body)
		}
		switch r.URL.Path {
		case "/v1/chat":
			stream := body["stream"] == true
			if body["temperature"] != float64(0) || body["top_p"] != float64(0) || body["max_tokens"] != float64(0) {
				t.Error("legal zeros", body)
			}
			if stream {
				if r.Header.Get("Accept") != "text/event-stream" {
					t.Error("stream accept")
				}
				fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"},\"finish_reason\":\"stop\"}]}\n")
			} else {
				if r.Header.Get("Accept") != "application/json" {
					t.Error("chat accept")
				}
				fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
			}
		case "/v1/embed":
			fmt.Fprint(w, `{"data":[{"index":0,"embedding":[0,1]}]}`)
		case "/v1/rerank":
			if body["query"] != "query" || body["top_n"] != float64(1) {
				t.Error("query/top", body)
			}
			fmt.Fprint(w, `{"results":[{"index":0,"relevance_score":0}]}`)
		case "/v1/ocr":
			doc := body["document"].(map[string]any)
			if doc["type"] != "document_url" || !(doc["document_url"] == "https://files.invalid/doc.pdf" || strings.HasPrefix(doc["document_url"].(string), "data:application/pdf;base64,")) {
				t.Error("document", doc)
			}
			fmt.Fprint(w, `{"pages":[{"index":0,"markdown":"page"}]}`)
		case "/v1/parse":
			if body["model_version"] != "model" || body["url"] != "https://files.invalid/doc.pdf" {
				t.Error("parser", body)
			}
			fmt.Fprint(w, `{"code":0,"data":{"task_id":"job-1"}}`)
		default:
			t.Error("path", r.URL.Path)
		}
	}))
	m := newAI302ContractModel(srv.URL)
	name, key := " model ", " key "
	api := &APIConfig{APIKey: &key}
	zero, zint, yes, no := 0.0, 0, true, false
	cfg := &ChatConfig{Stream: &yes, Temperature: &zero, TopP: &zero, MaxTokens: &zint}
	if _, err := m.ChatWithMessages(name, api, []Message{{Role: "user", Content: "q"}}, cfg); err != nil {
		t.Fatal(err)
	}
	cfg.Stream = &no
	if err := m.ChatStreamlyWithMessages(name, []Message{{Role: "user", Content: "q"}}, api, cfg, func(*string, *string) error { return nil }); err != nil {
		t.Fatal(err)
	}
	if _, err := m.Embed(&name, []string{"q"}, api, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := m.Rerank(&name, " query ", []string{"q"}, api, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := m.TranscribeAudio(&name, []byte("audio"), api, &ASRConfig{Params: map[string]any{"temperature": 0}}); err != nil {
		t.Fatal(err)
	}
	if _, err := m.OCRFile(&name, []byte("%PDF-1.7 owned"), api, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := m.OCRURL(&name, " https://files.invalid/doc.pdf ", api, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := m.ParseFile(&name, nil, " https://files.invalid/doc.pdf ", api); err != nil {
		t.Fatal(err)
	}
	if _, err := m.ShowTask(" job-1 ", api); err != nil {
		t.Fatal(err)
	}
	if result, err := m.ListModels(api); err != nil || result == nil || len(result) != 0 {
		t.Fatal(result, err)
	}
	if calls.Load() != 10 || name != " model " || key != " key " || cfg.Stream != &no {
		t.Fatal("request count or caller mutation", calls.Load())
	}
}

type ai302Call func(*AI302Model, *string, *APIConfig) error

func ai302ContractCalls() map[string]ai302Call {
	return map[string]ai302Call{
		"chat": func(m *AI302Model, n *string, a *APIConfig) error {
			name := ""
			if n != nil {
				name = *n
			}
			_, e := m.ChatWithMessages(name, a, []Message{{Role: "user", Content: "q"}}, nil)
			return e
		},
		"stream": func(m *AI302Model, n *string, a *APIConfig) error {
			return m.ChatStreamlyWithSender(n, stringPtr("q"), a, nil, func(*string, *string) error { return nil })
		},
		"embed": func(m *AI302Model, n *string, a *APIConfig) error {
			_, e := m.Embed(n, []string{"q"}, a, nil)
			return e
		},
		"rerank": func(m *AI302Model, n *string, a *APIConfig) error {
			_, e := m.Rerank(n, "q", []string{"q"}, a, nil)
			return e
		},
		"asr": func(m *AI302Model, n *string, a *APIConfig) error {
			_, e := m.TranscribeAudio(n, []byte("audio"), a, nil)
			return e
		},
		"ocr": func(m *AI302Model, n *string, a *APIConfig) error {
			_, e := m.OCRFile(n, []byte("pdf"), a, nil)
			return e
		},
		"parse": func(m *AI302Model, n *string, a *APIConfig) error {
			_, e := m.ParseFile(n, nil, "https://files.invalid/a.pdf", a)
			return e
		},
	}
}

func TestAI302DenialsHaveNoHTTP(t *testing.T) {
	var calls atomic.Int32
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1) }))
	m := newAI302ContractModel(srv.URL)
	api := &APIConfig{APIKey: stringPtr("key")}
	for label, call := range ai302ContractCalls() {
		t.Run(label, func(t *testing.T) {
			for _, name := range []*string{nil, stringPtr(" \t ")} {
				if err := call(m, name, api); err == nil {
					t.Fatal("invalid model")
				}
			}
			for _, bad := range []*APIConfig{nil, {}, {APIKey: stringPtr(" ")}, {APIKey: stringPtr("key"), Region: stringPtr("missing")}} {
				if err := call(m, stringPtr("model"), bad); err == nil {
					t.Fatal("invalid credentials/region")
				}
			}
		})
	}
	for _, raw := range []string{"", " ", "file:///tmp/a", "ftp://files.invalid/a", "https:///a", "https://user:password@files.invalid/a", "https://files.invalid/a#fragment"} {
		if _, err := m.OCRURL(stringPtr("model"), raw, api, nil); err == nil {
			t.Fatal("invalid OCR URL", raw)
		}
		if _, err := m.ParseFile(stringPtr("model"), nil, raw, api); err == nil {
			t.Fatal("invalid parser URL", raw)
		}
	}
	for _, id := range []string{"", " ", "../secret", "a/b", "a?x=1", "a#fragment"} {
		if _, err := m.ShowTask(id, api); err == nil {
			t.Fatal("invalid task", id)
		}
	}
	if _, err := m.TranscribeAudio(stringPtr("model"), nil, api, nil); err == nil {
		t.Fatal("missing audio")
	}
	if _, err := m.OCRFile(stringPtr("model"), nil, api, nil); err == nil {
		t.Fatal("missing OCR bytes")
	}
	if _, err := m.ParseFile(stringPtr("model"), []byte("pdf"), "https://files.invalid/a", api); err == nil {
		t.Fatal("ambiguous parser input")
	}
	if _, err := m.Rerank(stringPtr("model"), " ", []string{"x"}, api, nil); err == nil {
		t.Fatal("blank query")
	}
	if err := m.ChatStreamlyWithMessages("model", []Message{{Role: "user", Content: "q"}}, api, nil, nil); err == nil {
		t.Fatal("nil sender")
	}
	if _, err := m.ChatWithMessages("model", api, nil, nil); err == nil {
		t.Fatal("no messages")
	}
	for _, bad := range []*APIConfig{nil, {}, {APIKey: stringPtr(" ")}} {
		if _, err := m.ListModels(bad); err == nil {
			t.Fatal("inventory without credential")
		}
	}
	blank := NewAI302Model(map[string]string{"default": srv.URL}, URLSuffix{Chat: " / ", Models: " / ", Embedding: " / ", Rerank: " / ", ASR: " / ", OCR: " / ", DocumentParse: " / ", Task: " / "})
	for label, call := range ai302ContractCalls() {
		if err := call(blank, stringPtr("model"), api); err == nil {
			t.Fatal("blank endpoint", label)
		}
	}
	if _, err := blank.ListModels(api); err == nil {
		t.Fatal("blank inventory endpoint")
	}
	if result, err := m.Embed(nil, nil, nil, nil); err != nil || result == nil {
		t.Fatal(result, err)
	}
	if _, err := m.Rerank(nil, "", nil, nil, nil); err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 0 {
		t.Fatal("denial consumed quota", calls.Load())
	}
}

func TestAI302CancellationAndFailure(t *testing.T) {
	calls := ai302ContractCalls()
	calls["models"] = func(m *AI302Model, _ *string, a *APIConfig) error { _, e := m.ListModels(a); return e }
	calls["task"] = func(m *AI302Model, _ *string, a *APIConfig) error { _, e := m.ShowTask("job-1", a); return e }
	for label, call := range calls {
		t.Run(label, func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				io.Copy(io.Discard, r.Body)
				w.WriteHeader(200)
				w.(http.Flusher).Flush()
				<-r.Context().Done()
			}))
			ctx, cancel := context.WithTimeout(t.Context(), 40*time.Millisecond)
			defer cancel()
			err := call(newAI302ContractModel(srv.URL), stringPtr("model"), &APIConfig{APIKey: stringPtr("key"), Context: ctx})
			if !errors.Is(err, context.DeadlineExceeded) {
				t.Fatal("cancellation identity lost", err)
			}
		})
	}
	for _, status := range []int{200, 401, 429, 500} {
		t.Run(fmt.Sprint(status), func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(status)
				fmt.Fprint(w, `{"error":"private-provider-body"}`)
			}))
			for label, call := range calls {
				if err := call(newAI302ContractModel(srv.URL), stringPtr("model"), &APIConfig{APIKey: stringPtr("private-key")}); err == nil || strings.Contains(err.Error(), "private-provider-body") || strings.Contains(err.Error(), "private-key") {
					t.Fatal(label, err)
				}
			}
		})
	}
	sentinel := errors.New("sender failed")
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n")
	}))
	if err := newAI302ContractModel(srv.URL).ChatStreamlyWithMessages("model", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: stringPtr("key")}, nil, func(*string, *string) error { return sentinel }); !errors.Is(err, sentinel) {
		t.Fatal(err)
	}
}

func TestAI302StrictInventory(t *testing.T) {
	for _, raw := range []string{`{}`, `{"data":null}`, `{"data":[null]}`, `{"data":[{"id":null}]}`, `{"data":[{"id":4}]}`, `{"data":[{"id":" model"}]}`, `{"data":[{"id":"same"},{"id":"same"}]}`, `{"data":[],"error":{}}`, `{"data":[],"code":1}`, `{"data":[]} {}`} {
		t.Run(raw, func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, raw) }))
			if result, err := newAI302ContractModel(srv.URL).ListModels(&APIConfig{APIKey: stringPtr("key")}); err == nil || result != nil {
				t.Fatal(result, err)
			}
		})
	}
}

func TestAI302ConcurrentInstanceIsolation(t *testing.T) {
	var calls atomic.Int32
	var models []*AI302Model
	for _, identity := range []string{"one", "two"} {
		srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls.Add(1)
			var body map[string]any
			if json.NewDecoder(r.Body).Decode(&body) != nil || body["model"] != identity || r.Header.Get("Authorization") != "Bearer "+identity {
				t.Error("instance credentials/model crossed", identity, body)
			}
			fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
		}))
		urls := map[string]string{"default": srv.URL}
		models = append(models, NewAI302Model(urls, URLSuffix{Chat: "chat"}))
		urls["default"] = "http://mutated.invalid"
	}
	var group sync.WaitGroup
	for index, identity := range []string{"one", "two"} {
		name, key := " "+identity+" ", " "+identity+" "
		api := &APIConfig{APIKey: &key}
		for range 8 {
			group.Go(func() {
				if _, err := models[index].ChatWithMessages(name, api, []Message{{Role: "user", Content: "q"}}, nil); err != nil {
					t.Error(err)
				}
			})
		}
	}
	group.Wait()
	if calls.Load() != 16 {
		t.Fatal("wrong request count", calls.Load())
	}
}

func TestAI302MalformedResponses(t *testing.T) {
	bodies := map[string][]string{
		"chat":   {`{}`, `{"choices":[]}`, `{"choices":[null]}`, `{"choices":[{"message":{"content":null}}]}`, `{"choices":[{"message":{"content":4}}]}`},
		"stream": {"data: {}\n", "data: {\"choices\":[]}\n", "data: {\"choices\":[null]}\n", "data: malformed\n", "data: [DONE]\n"},
		"embed":  {`{"data":[]}`, `{"data":[{"index":0,"embedding":[null]}]}`, `{"data":[{"index":0,"embedding":[1e999]}]}`, `{"data":[{"embedding":[0]}]}`, `{"data":[{"index":1,"embedding":[0]}]}`},
		"rerank": {`{"results":[]}`, `{"results":[{"index":0,"relevance_score":null}]}`, `{"results":[{"index":0,"relevance_score":1e999}]}`, `{"results":[{"relevance_score":0}]}`, `{"results":[{"index":2,"relevance_score":0}]}`},
		"asr":    {`{}`, `{"text":null}`, `{"text":4}`, `{"text":"ok"} {}`},
		"ocr":    {`{}`, `{"pages":[null]}`, `{"pages":[{"index":null,"markdown":"page"}]}`, `{"pages":[{"index":0,"markdown":null}]}`, `{"pages":[{"index":0,"markdown":"a"},{"index":0,"markdown":"b"}]}`},
		"parse":  {`{}`, `{"code":null,"data":{"task_id":"job-1"}}`, `{"code":0,"data":{"task_id":"../secret"}}`, `{"code":0,"data":{"task_id":"job-1"}} {}`},
	}
	calls := ai302ContractCalls()
	for kind, responses := range bodies {
		for index, raw := range responses {
			t.Run(fmt.Sprintf("%s/%d", kind, index), func(t *testing.T) {
				srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, raw) }))
				if err := calls[kind](newAI302ContractModel(srv.URL), stringPtr("model"), &APIConfig{APIKey: stringPtr("key")}); err == nil {
					t.Fatal("malformed raw response accepted", raw)
				}
			})
		}
	}
}

func TestHostedStreamIdleDeadlines(t *testing.T) {
	for _, provider := range []string{"302.AI", "LongCat"} {
		for _, active := range []bool{false, true} {
			t.Run(fmt.Sprint(provider, "/active=", active), func(t *testing.T) {
				srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					io.Copy(io.Discard, r.Body)
					w.Header().Set("Content-Type", "text/event-stream")
					if !active {
						fmt.Fprint(w, "data: {")
						w.(http.Flusher).Flush()
						<-r.Context().Done()
						return
					}
					for range 8 {
						fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"x\"}}]}\n")
						w.(http.Flusher).Flush()
						select {
						case <-r.Context().Done():
							return
						case <-time.After(20 * time.Millisecond):
						}
					}
					fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n")
				}))
				var driver *openAIChatDriver
				if provider == "302.AI" {
					driver = NewAI302Model(map[string]string{"default": srv.URL}, URLSuffix{Chat: "chat"}).openAIChatDriver
				} else {
					driver = NewLongCatModel(map[string]string{"default": srv.URL}, URLSuffix{Chat: "chat"}).openAIChatDriver
				}
				if driver.streamIdleTimeout != 60*time.Second {
					t.Fatal("default idle deadline missing")
				}
				driver.streamIdleTimeout = 80 * time.Millisecond
				ctx, cancel := context.WithTimeout(t.Context(), 2*time.Second)
				defer cancel()
				api := &APIConfig{APIKey: stringPtr("key"), Context: ctx}
				answer := ""
				err := driver.ChatStreamlyWithMessages("model", []Message{{Role: "user", Content: "q"}}, api, nil, func(content, reason *string) error {
					if content != nil {
						answer += *content
					}
					return nil
				})
				if active {
					if err != nil || answer != "xxxxxxxx[DONE]" {
						t.Fatal(answer, err)
					}
				} else if !errors.Is(err, context.DeadlineExceeded) || strings.Contains(answer, "[DONE]") {
					t.Fatal("idle cancellation lost", answer, err)
				}
				if api.Context != ctx {
					t.Fatal("caller context mutated")
				}
			})
		}
	}
}

func TestAI302RejectsMalformedFramesAfterAnswer(t *testing.T) {
	for _, test := range []struct {
		name, frame string
		valid       bool
	}{
		{"null-choice", `{"choices":[null]}`, false},
		{"null-delta", `{"choices":[{"delta":null}]}`, false},
		{"missing-delta", `{"choices":[{}]}`, false},
		{"invalid-terminal", `{"choices":[{"delta":null,"finish_reason":"content_filter"}]}`, false},
		{"valid-terminal-without-delta", `{"choices":[{"finish_reason":"stop"}]}`, true},
		{"valid-terminal-with-null-delta", `{"choices":[{"delta":null,"finish_reason":"stop"}]}`, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":\"prefix\"}}]}\ndata: %s\ndata: [DONE]\n", test.frame)
			}))
			model := NewAI302Model(map[string]string{"default": srv.URL}, URLSuffix{Chat: "chat"})
			answer := ""
			err := model.ChatStreamlyWithMessages("model", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: stringPtr("key")}, nil, func(content, reason *string) error {
				if content != nil {
					answer += *content
				}
				return nil
			})
			if test.valid {
				if err != nil || answer != "prefix[DONE]" {
					t.Fatal(answer, err)
				}
			} else if err == nil || answer != "prefix" {
				t.Fatal("malformed frame after valid answer became successful completion", answer, err)
			}
		})
	}
}
