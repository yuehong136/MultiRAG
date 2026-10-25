package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

var after0fbJournalMu sync.Mutex

func after0fbRecord(t *testing.T, row map[string]any) {
	t.Helper()
	row["case"] = t.Name()
	row["run_id"] = os.Getenv("MULTIRAG_AFTER0FB_RUN_ID") + ":" + t.Name()
	row["utc"] = time.Now().UTC().Format(time.RFC3339Nano)
	path := os.Getenv("MULTIRAG_AFTER0FB_B_RESOURCES")
	if path == "" {
		t.Log(row)
		return
	}
	after0fbJournalMu.Lock()
	defer after0fbJournalMu.Unlock()
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if err := json.NewEncoder(f).Encode(row); err != nil {
		t.Fatal(err)
	}
}
func after0fbServer(t *testing.T, handler http.Handler) *httptest.Server {
	t.Helper()
	id := fmt.Sprintf("%s-%d", t.Name(), time.Now().UnixNano())
	after0fbRecord(t, map[string]any{"action": "intent", "kind": "http-listener", "id": id, "listen": "127.0.0.1:0"})
	srv := httptest.NewUnstartedServer(handler)
	address := srv.Listener.Addr().String()
	after0fbRecord(t, map[string]any{"action": "bound", "id": id, "address": address})
	srv.Start()
	t.Cleanup(func() {
		srv.CloseClientConnections()
		srv.Close()
		conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
		if err == nil {
			conn.Close()
			t.Error("listener remains", address)
		}
		after0fbRecord(t, map[string]any{"action": "closed-readback", "id": id, "address": address, "absent": err != nil})
	})
	return srv
}
func after0fbDriver(t *testing.T, provider, base string) ModelDriver {
	t.Helper()
	d, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": base}, URLSuffix{Chat: "chat", AsyncChat: "v1chat", Models: "models", Embedding: "embed", Rerank: "rerank", TTS: "tts"})
	if err != nil || d.Name() != strings.ToLower(provider) {
		t.Fatal(d, err)
	}
	return d
}
func after0fbCore(d ModelDriver) *openAIChatDriver {
	switch v := d.(type) {
	case *HuaweiCloudModel:
		return v.openAIChatDriver
	case *ModelScopeModel:
		return v.openAIChatDriver
	case *OrcaRouterModel:
		return v.openAIChatDriver
	case *QiniuModel:
		return v.openAIChatDriver
	}
	panic("unexpected test provider")
}
func TestAfter0fbNewProviderWire(t *testing.T) {
	for _, provider := range []string{"HuaweiCloud", "ModelScope", "OrcaRouter", "Qiniu"} {
		t.Run(provider, func(t *testing.T) {
			response := `{"choices":[{"message":{"content":"answer","reasoning_content":"reason"}}]}`
			var calls atomic.Int32
			var last map[string]any
			key, name, zero := "key", "served", 0.0
			api := &APIConfig{APIKey: &key}
			srv := after0fbServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.Header.Get("Authorization") != "Bearer key" {
					t.Error("lost credentials")
				}
				if r.Method == http.MethodGet {
					if r.ContentLength > 0 {
						t.Error("GET body")
					}
				} else {
					if err := json.NewDecoder(r.Body).Decode(&last); err != nil {
						t.Error(err)
					}
					if last["model"] != name || last["temperature"] != float64(0) || last["top_p"] != float64(0) {
						t.Error("lost zero/model", last)
					}
				}
				fmt.Fprint(w, response)
			}))
			d := after0fbDriver(t, provider, srv.URL)
			cfg := &ChatConfig{Temperature: &zero, TopP: &zero}
			messages := []Message{{Role: "system", Content: "rules"}, {Role: "assistant", Content: "prior"}, {Role: "user", Content: "question"}}
			out, err := d.ChatWithMessages(name, api, messages, cfg)
			if err != nil || out == nil || *out.Answer != "answer" || *out.ReasoningContent != "reason" {
				t.Fatal(out, err)
			}
			if len(last["messages"].([]any)) != 3 {
				t.Fatal("history lost", last)
			}
			for _, raw := range []string{`{}`, `{"choices":[]}`, `{"choices":[null]}`, `{"choices":[{"message":{"content":42}}]}`, `{"choices":[{"message":{"content":null}}]}`, `{"choices":[{"message":{"content":"answer","reasoning_content":42}}]}`, `{"error":"private-provider-body"}`, `{"code":7,"choices":[{"message":{"content":"answer"}}]}`, `{"choices":[{"message":{"content":"ok"}}]} trailing`} {
				response = raw
				if out, err := d.ChatWithMessages(name, api, messages, cfg); err == nil || out != nil || strings.Contains(err.Error(), "private-provider-body") {
					t.Fatal("accepted raw chat", raw, out, err)
				}
			}
			response = "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"reason\"}}]}\n\ndata: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: {\"choices\":[{\"finish_reason\":\"stop\"}]}\n\n"
			var answer, reason string
			err = d.ChatStreamlyWithMessages(name, messages, api, cfg, func(a, r *string) error {
				if a != nil {
					answer += *a
				}
				if r != nil {
					reason += *r
				}
				return nil
			})
			if err != nil || answer != "answer[DONE]" || reason != "reason" {
				t.Fatal(answer, reason, err)
			}
			for _, raw := range []string{"data: {oops}\n\n", "data: {\"choices\":[]}\n\n", "data: {\"choices\":[{\"delta\":{\"content\":42}}]}\n\n", "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n", "data: {\"error\":\"private-provider-body\"}\n\n", "data: [DONE]\n\n", "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"},\"finish_reason\":\"content_filter\"}]}\n\n", "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]"} {
				response = raw
				done := false
				err = d.ChatStreamlyWithMessages(name, messages, api, cfg, func(a, r *string) error { done = done || a != nil && *a == "[DONE]"; return nil })
				if err == nil || done || strings.Contains(err.Error(), "private-provider-body") {
					t.Fatal("false completion", raw, done, err)
				}
			}
			response = "data: {\"choices\":[{\"delta\":{\"content\":\"answer\",\"reasoning_content\":\"reason\"},\"finish_reason\":\"stop\"}]}\n\n"
			want := errors.New("sender failed")
			if err := d.ChatStreamlyWithMessages(name, messages, api, cfg, func(*string, *string) error { return want }); !errors.Is(err, want) {
				t.Fatal(err)
			}
			ctx, cancel := context.WithCancel(t.Context())
			n := 0
			err = d.ChatStreamlyWithMessages(name, messages, &APIConfig{APIKey: &key, Context: ctx}, cfg, func(*string, *string) error { n++; cancel(); return nil })
			if !errors.Is(err, context.Canceled) || n != 1 {
				t.Fatal("cancel continued", n, err)
			}
			for _, raw := range []string{`{"data":[]}`, `{"data":[{"id":"served"}]}`, `{}`, `{"data":null}`, `{"data":[{}]}`, `{"data":[{"id":null}]}`, `{"data":[{"id":42}]}`, `{"data":[{"id":" padded "}]}`, `{"data":[{"id":"same"},{"id":"same"}]}`, `{"data":[{"id":"ok"},null]}`, `{"error":"private-provider-body","data":[]}`} {
				response = raw
				valid := raw == `{"data":[]}` || raw == `{"data":[{"id":"served"}]}`
				_, err := d.ListModels(api)
				if valid != (err == nil) {
					t.Fatal(raw, err)
				}
			}
			before := calls.Load()
			f := false
			missing := "missing"
			negative := -1
			bad := math.NaN()
			for _, cfg := range []*ChatConfig{{Stream: &f}, {Temperature: &bad}, {MaxTokens: &negative}} {
				if err := d.ChatStreamlyWithMessages(name, messages, api, cfg, func(*string, *string) error { return nil }); err == nil {
					t.Fatal("bad config")
				}
			}
			if _, err := d.ChatWithMessages("", api, messages, nil); err == nil {
				t.Fatal("empty model")
			}
			if _, err := d.ChatWithMessages(name, api, []Message{{Role: "user", Content: 42}}, nil); err == nil {
				t.Fatal("invalid message")
			}
			if _, err := d.ListModels(&APIConfig{APIKey: &key, Region: &missing}); err == nil {
				t.Fatal("missing region")
			}
			if provider != "ModelScope" {
				if _, err := d.ListModels(&APIConfig{}); err == nil {
					t.Fatal("missing key")
				}
			}
			if calls.Load() != before {
				t.Fatal("invalid request side effects", calls.Load(), before)
			}
			if provider != "HuaweiCloud" {
				if _, err := d.Embed(&name, []string{"a"}, api, nil); err == nil {
					t.Fatal("unsupported embed")
				}
				if _, err := d.Rerank(&name, "q", []string{"a"}, api, nil); err == nil {
					t.Fatal("unsupported rerank")
				}
			}
			if _, ok := d.(TTSDriver); ok != (provider == "OrcaRouter") {
				t.Fatal("incorrect speech capability")
			}
		})
	}
}

func TestAfter0fbThinkingAndHuaweiPaths(t *testing.T) {
	for _, tc := range []struct {
		provider, name, effort, path, wire, field string
		thinking                                  bool
		want                                      any
	}{
		{"HuaweiCloud", "DeepSeek-V3", "high", "/v1chat", "deepseek-v3", "thinking", true, nil},
		{"HuaweiCloud", "deepseek-v4-pro", "max", "/chat", "deepseek-v4-pro", "thinking", true, map[string]any{"type": "enabled"}},
		{"HuaweiCloud", "deepseek-v4-pro", "low", "/chat", "deepseek-v4-pro", "thinking", true, map[string]any{"type": "disabled"}},
		{"HuaweiCloud", "qwen3-32b", "high", "/chat", "qwen3-32b", "thinking", false, map[string]any{"type": "disabled"}},
		{"HuaweiCloud", "deepseek-r1-250528", "high", "/chat", "deepseek-r1-250528", "thinking", true, nil},
		{"Qiniu", "qwen3-32b", "none", "/chat", "qwen3-32b", "enable_thinking", true, false},
		{"Qiniu", "qwen3-32b", "high", "/chat", "qwen3-32b", "enable_thinking", true, true},
		{"Qiniu", "deepseek/deepseek-v4-pro", "max", "/chat", "deepseek/deepseek-v4-pro", "thinking", true, map[string]any{"type": "enabled"}},
		{"Qiniu", "deepseek/deepseek-v4-pro", "high", "/chat", "deepseek/deepseek-v4-pro", "thinking", false, map[string]any{"type": "disabled"}},
		{"Qiniu", "plain", "high", "/chat", "plain", "thinking", true, nil},
	} {
		t.Run(tc.provider+tc.name+tc.effort+fmt.Sprint(tc.thinking), func(t *testing.T) {
			srv := after0fbServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var b map[string]any
				if err := json.NewDecoder(r.Body).Decode(&b); err != nil {
					t.Error(err)
				}
				if r.URL.Path != tc.path || b["model"] != tc.wire || !reflect.DeepEqual(b[tc.field], tc.want) {
					t.Error("wrong shape", r.URL.Path, b, tc)
				}
				if tc.provider == "HuaweiCloud" && tc.effort == "max" && b["reasoning_effort"] != "max" {
					t.Error("Huawei effort", b)
				}
				if tc.provider == "Qiniu" && tc.effort == "max" && b["reasoning_effort"] != "high" {
					t.Error("Qiniu effort", b)
				}
				fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
			}))
			d := after0fbDriver(t, tc.provider, srv.URL)
			key := "Bearer key"
			if tc.provider != "HuaweiCloud" {
				key = "key"
			}
			if _, err := d.ChatWithMessages(tc.name, &APIConfig{APIKey: &key}, []Message{{Role: "user", Content: "q"}}, &ChatConfig{Thinking: &tc.thinking, Effort: &tc.effort}); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestAfter0fbModelScopeOptionalAuthAndAliases(t *testing.T) {
	for _, field := range []string{"reasoning_content", "reasoning", "thinking"} {
		t.Run(field, func(t *testing.T) {
			stream := false
			key := ""
			srv := after0fbServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				want := ""
				if key != "" {
					want = "Bearer " + key
				}
				if r.Header.Get("Authorization") != want {
					t.Error("auth")
				}
				if r.URL.Path != "/v1/chat/completions" {
					t.Error("normalization", r.URL.Path)
				}
				if stream {
					fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\",%q:\"reason\"},\"finish_reason\":\"stop\"}]}\n\n", field)
				} else {
					fmt.Fprintf(w, `{"choices":[{"message":{"content":"answer",%q:"reason"}}]}`, field)
				}
			}))
			d := NewModelScopeModel(map[string]string{"default": srv.URL + "/v1/"}, URLSuffix{Chat: "v1/chat/completions"})
			for _, auth := range []string{"", "key"} {
				key = auth
				out, err := d.ChatWithMessages("tenant", &APIConfig{APIKey: &key}, []Message{{Role: "user", Content: "q"}}, nil)
				if err != nil || out.ReasoningContent == nil || *out.ReasoningContent != "reason" {
					t.Fatal(out, err)
				}
			}
			stream = true
			reason := ""
			err := d.ChatStreamlyWithMessages("tenant", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: &key}, nil, func(a, r *string) error {
				if r != nil {
					reason += *r
				}
				return nil
			})
			if err != nil || reason != "reason" {
				t.Fatal(reason, err)
			}
		})
	}
	original := http.DefaultTransport
	http.DefaultTransport = after0fbRoundTripper(func(*http.Request) (*http.Response, error) { return nil, errors.New("custom") })
	t.Cleanup(func() { http.DefaultTransport = original })
	if NewModelScopeModel(nil, URLSuffix{}) == nil {
		t.Fatal("custom transport panic")
	}
}

type after0fbRoundTripper func(*http.Request) (*http.Response, error)

func (f after0fbRoundTripper) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestAfter0fbNewProviderDeadlineIsolation(t *testing.T) {
	for _, provider := range []string{"HuaweiCloud", "ModelScope", "OrcaRouter", "Qiniu"} {
		t.Run(provider, func(t *testing.T) {
			for _, stream := range []bool{false, true} {
				t.Run(fmt.Sprint(stream), func(t *testing.T) {
					srv := after0fbServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						w.WriteHeader(200)
						w.(http.Flusher).Flush()
						<-r.Context().Done()
					}))
					d := after0fbDriver(t, provider, srv.URL)
					key := "key"
					ctx, cancel := context.WithTimeout(t.Context(), 40*time.Millisecond)
					defer cancel()
					api := &APIConfig{APIKey: &key, Context: ctx}
					var err error
					if stream {
						err = d.ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "q"}}, api, nil, func(*string, *string) error { return nil })
					} else {
						_, err = d.ChatWithMessages("served", api, []Message{{Role: "user", Content: "q"}}, nil)
					}
					if !errors.Is(err, context.DeadlineExceeded) {
						t.Fatal(err)
					}
					if stream {
						after0fbCore(d).streamIdleTimeout = 30 * time.Millisecond
						err = d.ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: &key}, nil, func(*string, *string) error { return nil })
						if !errors.Is(err, context.DeadlineExceeded) {
							t.Fatal("unbounded idle", err)
						}
					}
				})
			}
			urls := map[string]string{"default": "http://unused"}
			original := after0fbDriver(t, provider, urls["default"])

			for _, instance := range []string{"one", "two"} {
				t.Run(instance, func(t *testing.T) {
					t.Parallel()
					srv := after0fbServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						if r.Header.Get("Authorization") != "Bearer "+instance {
							t.Error("crossed credentials")
						}
						fmt.Fprintf(w, `{"choices":[{"message":{"content":%q}}]}`, instance)
					}))
					localURLs := map[string]string{"default": srv.URL}
					d := original.(interface {
						NewInstance(map[string]string) ModelDriver
					}).NewInstance(localURLs)
					localURLs["default"] = "http://mutated"
					out, err := d.ChatWithMessages("served", &APIConfig{APIKey: &instance}, []Message{{Role: "user", Content: "q"}}, nil)
					if err != nil || out == nil || *out.Answer != instance {
						t.Fatal(out, err)
					}
				})
			}

			if after0fbCore(original).BaseURL["default"] != "http://unused" {
				t.Fatal("mutated prototype")
			}
		})
	}
}

func TestAfter0fbHuaweiVectorsAndOrcaSpeech(t *testing.T) {
	t.Run("Huawei", func(t *testing.T) {
		response := `{"data":[{"index":1,"embedding":[1,0]},{"index":0,"embedding":[0,1]}]}`
		var calls atomic.Int32
		srv := after0fbServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls.Add(1)
			if r.Header.Get("Authorization") != "Bearer key" {
				t.Error("auth")
			}
			fmt.Fprint(w, response)
		}))
		d := after0fbDriver(t, "HuaweiCloud", srv.URL)
		key, name := "Bearer key", "model"
		api := &APIConfig{APIKey: &key}
		out, err := d.Embed(&name, []string{"a", "b"}, api, &EmbeddingConfig{Dimension: 2})
		if err != nil || !reflect.DeepEqual(out, []EmbeddingData{{Index: 0, Embedding: []float64{0, 1}}, {Index: 1, Embedding: []float64{1, 0}}}) {
			t.Fatal(out, err)
		}
		for _, raw := range []string{`{}`, `{"data":[]}`, `{"data":[{"embedding":[1,0]},{"index":1,"embedding":[1,0]}]}`, `{"data":[{"index":0,"embedding":[null,0]},{"index":1,"embedding":[1,0]}]}`, `{"data":[{"index":0,"embedding":[1]},{"index":1,"embedding":[1,0]}]}`, `{"data":[{"index":0,"embedding":[1,0]},{"index":0,"embedding":[1,0]}]}`, `{"data":[{"index":0,"embedding":[1,0]},{"index":2,"embedding":[1,0]}]}`, `{"data":[{"index":0,"embedding":[1,0]},{"index":1,"embedding":[1e999,0]}]}`} {
			response = raw
			if out, err := d.Embed(&name, []string{"a", "b"}, api, nil); err == nil || out != nil {
				t.Fatal("raw embedding accepted", raw, out, err)
			}
		}
		before := calls.Load()
		if out, err := d.Embed(&name, nil, api, nil); err != nil || len(out) != 0 || calls.Load() != before {
			t.Fatal("empty embed I/O", out, err)
		}
		response = `{"results":[{"index":1,"relevance_score":0},{"index":0,"relevance_score":0.8}]}`
		scores, err := NewRerankModel(d, &name, api).Rerank("q", []string{"a", "b"})
		if err != nil || !reflect.DeepEqual(scores, []float64{0.8, 0}) {
			t.Fatal(scores, err)
		}
		outRank, err := d.Rerank(&name, "q", []string{"a", "b"}, api, &RerankConfig{TopN: 1})
		if err != nil || len(outRank.Data) != 1 || outRank.Data[0].Index != 0 {
			t.Fatal(outRank, err)
		}
		for _, raw := range []string{`{}`, `{"results":[]}`, `{"results":[{"index":0,"relevance_score":0.8},{"index":1,"relevance_score":null}]}`, `{"results":[{"index":0,"relevance_score":0.8},{"index":0,"relevance_score":0}]}`, `{"results":[{"index":0,"relevance_score":0.8},{"index":2,"relevance_score":0}]}`, `{"results":[{"index":0,"relevance_score":0.8},{"index":1,"relevance_score":1e999}]}`} {
			response = raw
			if out, err := d.Rerank(&name, "q", []string{"a", "b"}, api, &RerankConfig{TopN: 1}); err == nil || out != nil {
				t.Fatal("bad tail bypassed TopN", raw, out, err)
			}
		}
	})
	t.Run("OrcaSpeech", func(t *testing.T) {
		body, mime, status := "\x00audio\xff", "audio/mpeg", 200
		var calls atomic.Int32
		srv := after0fbServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls.Add(1)
			var b map[string]any
			json.NewDecoder(r.Body).Decode(&b)
			if b["model"] != "voice" || b["input"] != "hello" || b["voice"] != "alloy" || b["stream"] != false || b["response_format"] != "mp3" {
				t.Error(b)
			}
			w.Header().Set("Content-Type", mime)
			w.WriteHeader(status)
			fmt.Fprint(w, body)
		}))
		d := after0fbDriver(t, "OrcaRouter", srv.URL).(TTSDriver)
		name, key := "voice", "key"
		api := &APIConfig{APIKey: &key}
		cfg := &TTSConfig{Voice: "alloy", Format: "mp3"}
		out, err := d.AudioSpeech(&name, "hello", api, cfg)
		if err != nil || string(out.Audio) != body || out.Format != "mp3" {
			t.Fatal(out, err)
		}
		for _, invalid := range []struct {
			body, mime string
			status     int
		}{{"", "audio/mpeg", 200}, {`{"error":"private-provider-body"}`, "application/json", 200}, {"wav", "audio/wav", 200}, {"private-provider-body", "audio/mpeg", 401}} {
			body, mime, status = invalid.body, invalid.mime, invalid.status
			if out, err := d.AudioSpeech(&name, "hello", api, cfg); err == nil || out != nil || strings.Contains(err.Error(), "private-provider-body") {
				t.Fatal(out, err)
			}
		}
		before := calls.Load()
		for _, params := range []map[string]any{{"model": "bad"}, {"input": "bad"}, {"stream": true}, {"api_key": "bad"}} {
			if _, err := d.AudioSpeech(&name, "hello", api, &TTSConfig{Voice: "alloy", Params: params}); err == nil {
				t.Fatal("identity override")
			}
		}
		if calls.Load() != before {
			t.Fatal("speech rejection had I/O")
		}
	})
}

func TestAfter0fbVectorAndSpeechInFlightCancellation(t *testing.T) {
	for _, operation := range []string{"embedding", "rerank", "speech"} {
		t.Run(operation, func(t *testing.T) {
			srv := after0fbServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if operation == "speech" {
					w.Header().Set("Content-Type", "audio/mpeg")
				}
				w.WriteHeader(200)
				w.(http.Flusher).Flush()
				<-r.Context().Done()
			}))
			ctx, cancel := context.WithTimeout(t.Context(), 40*time.Millisecond)
			defer cancel()
			name, key := "served", "key"
			api := &APIConfig{APIKey: &key, Context: ctx}
			d := after0fbDriver(t, "HuaweiCloud", srv.URL)
			var err error
			switch operation {
			case "embedding":
				_, err = d.Embed(&name, []string{"a"}, api, nil)
			case "rerank":
				_, err = d.Rerank(&name, "q", []string{"a"}, api, nil)
			case "speech":
				_, err = after0fbDriver(t, "OrcaRouter", srv.URL).(TTSDriver).AudioSpeech(&name, "hello", api, &TTSConfig{Voice: "alloy"})
			}
			if !errors.Is(err, context.DeadlineExceeded) {
				t.Fatal(err)
			}
		})
	}
}
