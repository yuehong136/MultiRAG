package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestNovitaThinkEveryBoundary(t *testing.T) {
	raw := "before <think>原因 😀</think> middle <think>reason</think> answer < x"
	for width := 1; width <= len(raw); width++ {
		extractor := &novitaThinkExtractor{}
		var a, r strings.Builder
		for start := 0; start < len(raw); start += width {
			for _, s := range extractor.Feed(raw[start:min(len(raw), start+width)]) {
				a.WriteString(s.content)
				r.WriteString(s.reasoning)
			}
		}
		if s := extractor.Flush(); s != nil {
			a.WriteString(s.content)
			r.WriteString(s.reasoning)
		}
		if a.String() != "before  middle  answer < x" || r.String() != "原因 😀reason" {
			t.Fatal(width, a.String(), r.String())
		}
	}
}

func TestNewChatProviderRequestAndReasoningHTTP(t *testing.T) {
	for _, provider := range []string{"Novita", "LongCat"} {
		t.Run(provider, func(t *testing.T) {
			var received map[string]any
			response := `{"choices":[{"message":{"content":"answer","reasoning_content":"separate"}}]}`
			if provider == "Novita" {
				response = `{"choices":[{"message":{"content":"<think>inline</think>answer","reasoning_content":"separate"}}]}`
			}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/v1/chat" || r.Header.Get("Authorization") != "Bearer key" {
					t.Error("URL/key")
				}
				if e := json.NewDecoder(r.Body).Decode(&received); e != nil {
					t.Error(e)
				}
				fmt.Fprint(w, response)
			}))
			defer server.Close()
			urls := map[string]string{"default": server.URL + "/v1/"}
			d, _ := NewModelFactory().CreateModelDriver(provider, urls, URLSuffix{Chat: "chat"})
			urls["default"] = "http://mutated.invalid"
			yes, no := true, false
			zero := 0.0
			maxTokens := 55
			topP := 0.8
			stop := []string{"stop"}
			cfg := &ChatConfig{Stream: &yes, Thinking: &no, Temperature: &zero, MaxTokens: &maxTokens, TopP: &topP, Stop: &stop}
			msgs := []Message{{Role: "system", Content: "rules"}, {Role: "user", Content: "old"}, {Role: "assistant", Content: "prior"}, {Role: "user", Content: "question"}}
			r, e := d.ChatWithMessages("model", &APIConfig{APIKey: stringPtr("key")}, msgs, cfg)
			wantReason := "separate"
			if provider == "Novita" {
				wantReason = "inline\nseparate"
			}
			if e != nil || *r.Answer != "answer" || *r.ReasoningContent != wantReason || received["stream"] != false || received["temperature"] != zero || received["max_tokens"] != float64(55) || len(received["messages"].([]any)) != 4 {
				t.Fatal(r, e, received)
			}
			if provider == "Novita" && received["enable_thinking"] != false {
				t.Fatal("explicit false missing", received)
			}
			if provider == "LongCat" && (received["stop"] != nil || received["enable_thinking"] != nil) {
				t.Fatal("undocumented options", received)
			}
		})
	}
}

func TestNewChatProviderStreamsHTTP(t *testing.T) {
	for _, provider := range []string{"Novita", "LongCat", "Mistral"} {
		t.Run(provider, func(t *testing.T) {
			content := "answer"
			reason := "reason"
			frame := func(c, r string) string {
				data, _ := json.Marshal(map[string]any{"choices": []any{map[string]any{"delta": map[string]any{"content": c, "reasoning_content": r}}}})
				return "data: " + string(data) + "\n\n"
			}
			happy := frame(content, reason)
			if provider == "Novita" {
				content = "中文😀answer"
				happy = frame("中文😀<t", "reason") + frame("hink>原因😀</th", "") + frame("ink>answer", "")
				reason += "原因😀"
			}
			for _, test := range []struct {
				name, events string
				valid        bool
			}{
				{"done", happy + "data: [DONE]\n\n", true},
				{"finish EOF", happy + "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n", true},
				{"late error", happy + "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\ndata: {\"error\":{\"message\":\"private-body\"}}\n\n", false},
				{"malformed", happy + "data: {\n\ndata: [DONE]\n\n", false},
				{"truncated", happy, false}, {"empty", "data: [DONE]\n\n", false},
				{"reasoning only", frame("", "reason") + "data: [DONE]\n\n", false},
			} {
				t.Run(test.name, func(t *testing.T) {
					server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						var b map[string]any
						if e := json.NewDecoder(r.Body).Decode(&b); e != nil {
							t.Error(e)
						}
						if b["stream"] != true {
							t.Error("stream flag")
						}
						fmt.Fprint(w, test.events)
					}))
					defer server.Close()
					d, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
					var answer, reasoning strings.Builder
					done := 0
					err := d.ChatStreamlyWithMessages("model", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: stringPtr("key")}, nil, func(a, r *string) error {
						if a != nil {
							if *a == "[DONE]" {
								done++
							} else {
								answer.WriteString(*a)
							}
						}
						if r != nil {
							reasoning.WriteString(*r)
						}
						return nil
					})
					if (err == nil) != test.valid || !test.valid && done != 0 || test.valid && (done != 1 || answer.String() != content || reasoning.String() != reason) {
						t.Fatal(answer.String(), reasoning.String(), done, err)
					}
					if err != nil && strings.Contains(err.Error(), "private-body") {
						t.Fatal("body leaked")
					}
					marker := errors.New("sender failed")
					if test.valid {
						e := d.ChatStreamlyWithSender(stringPtr("model"), stringPtr("q"), &APIConfig{APIKey: stringPtr("key")}, nil, func(a, r *string) error { return marker })
						if !errors.Is(e, marker) {
							t.Fatal(e)
						}
					}
				})
			}
		})
	}
}

func TestNewProviderCancellationAndUnsupported(t *testing.T) {
	for _, provider := range []string{"Novita", "LongCat", "Mistral", "Jina"} {
		t.Run(provider, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				r.Body.Close()
				w.Header().Set("Content-Type", "text/event-stream")
				w.WriteHeader(200)
				w.(http.Flusher).Flush()
				<-r.Context().Done()
			}))
			defer server.Close()
			d, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Chat: "chat", Models: "models", Embedding: "embeddings", Rerank: "rerank"})
			if provider != "Jina" {
				ctx, cancel := context.WithTimeout(context.Background(), 80*time.Millisecond)
				defer cancel()
				done := 0
				err := d.ChatStreamlyWithSender(stringPtr("model"), stringPtr("q"), &APIConfig{APIKey: stringPtr("key"), Context: ctx}, nil, func(a, r *string) error {
					if a != nil && *a == "[DONE]" {
						done++
					}
					return nil
				})
				if !errors.Is(err, context.DeadlineExceeded) || done != 0 {
					t.Fatal(err, done)
				}
			}
			if provider == "LongCat" {
				if _, e := d.ListModels(nil); e == nil {
					t.Fatal("discovery")
				}
				if e := d.CheckConnection(nil); e == nil {
					t.Fatal("connection")
				}
			}
			if provider == "Novita" || provider == "LongCat" {
				if provider == "LongCat" {
					if _, e := d.Embed(nil, nil, nil, nil); e == nil {
						t.Fatal("embedding")
					}
				} else if data, e := d.Embed(nil, nil, nil, nil); e != nil || data == nil || len(data) != 0 {
					t.Fatal("empty Novita embedding", data, e)
				}
				if provider == "LongCat" {
					if _, e := d.Rerank(nil, "", nil, nil, nil); e == nil {
						t.Fatal("rerank")
					}
				} else if data, e := d.Rerank(nil, "", nil, nil, nil); e != nil || data == nil || len(data.Data) != 0 {
					t.Fatal("empty Novita rerank", data, e)
				}
			}
			if _, e := d.Balance(nil); e == nil {
				t.Fatal("balance")
			}
			if _, ok := d.(OCRDriver); ok != (provider == "Mistral") {
				t.Fatal("OCR capability mismatch")
			}
			if _, ok := d.(ASRDriver); ok {
				t.Fatal("invented ASR")
			}
			if _, ok := d.(TTSDriver); ok {
				t.Fatal("invented TTS")
			}
			if ch, e := d.ChatStreamly(nil, nil, nil, nil); e == nil || !reflect.ValueOf(ch).IsNil() {
				t.Fatal("channel success")
			}
		})
	}
}

func TestMistralNovitaDiscoveryHTTP(t *testing.T) {
	for _, provider := range []string{"Mistral", "Novita"} {
		t.Run(provider, func(t *testing.T) {
			calls := 0
			fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				if r.URL.Path != "/instance/models" || r.Header.Get("Authorization") != "Bearer key" {
					t.Error("discovery URL/key")
				}
				fmt.Fprint(w, `{"data":[{"id":"callable-model"}]}`)
			}))
			defer fixture.Close()
			d, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": fixture.URL + "/instance/"}, URLSuffix{Models: "models"})
			api := &APIConfig{APIKey: stringPtr("key")}
			names, e := d.ListModels(api)
			if e != nil || !reflect.DeepEqual(names, []string{"callable-model"}) {
				t.Fatal(names, e)
			}
			if e := d.CheckConnection(api); e != nil {
				t.Fatal(e)
			}
			if _, e := d.ListModels(nil); e == nil {
				t.Fatal("missing key accepted")
			}
			if _, e := d.ListModels(&APIConfig{APIKey: stringPtr("key"), Region: stringPtr("unknown")}); e == nil {
				t.Fatal("unknown region accepted")
			}
			if calls != 2 {
				t.Fatal("preflight called supplier", calls)
			}
		})
	}
}

func TestNovitaThinkUnicodeSerializedSegments(t *testing.T) {
	for _, test := range []struct {
		name              string
		chunks            []string
		answer, reasoning string
	}{
		{"opening", []string{"中文😀<t", "hink>原因😀</think>答案"}, "中文😀答案", "原因😀"},
		{"closing", []string{"<think>中文😀</t", "hink>答案😀"}, "答案😀", "中文😀"},
		{"literal", []string{"中文😀<x", " 普通<", "文本😀"}, "中文😀<x 普通<文本😀", ""},
		{"flush outside", []string{"中文😀<t"}, "中文😀<t", ""},
		{"flush inside", []string{"<think>中文😀</t"}, "", "中文😀</t"},
	} {
		t.Run(test.name, func(t *testing.T) {
			extractor := &novitaThinkExtractor{}
			var a, r strings.Builder
			consume := func(segment novitaThinkSegment) {
				// This is the JSON boundary used by HTTP senders. Invalid UTF-8 would be
				// silently replaced by U+FFFD here even if raw concatenation looks correct.
				data, err := json.Marshal([]string{segment.content, segment.reasoning})
				if err != nil {
					t.Fatal(err)
				}
				var decoded []string
				if err := json.Unmarshal(data, &decoded); err != nil {
					t.Fatal(err)
				}
				a.WriteString(decoded[0])
				r.WriteString(decoded[1])
			}
			for _, chunk := range test.chunks {
				for _, segment := range extractor.Feed(chunk) {
					consume(segment)
				}
			}
			if tail := extractor.Flush(); tail != nil {
				consume(*tail)
			}
			if a.String() != test.answer || r.String() != test.reasoning {
				t.Fatalf("serialized answer=%q reasoning=%q", a.String(), r.String())
			}
		})
	}
}
