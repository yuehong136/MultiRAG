package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func anthropicTestPtr[T any](value T) *T { return &value }

func TestAnthropicNativeMessagesHTTP(t *testing.T) {
	for _, thinking := range []*bool{nil, anthropicTestPtr(false), anthropicTestPtr(true)} {
		t.Run(fmt.Sprint(thinking), func(t *testing.T) {
			fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != "POST" || r.URL.Path != "/office/v1/messages" || r.Header.Get("x-api-key") != "fixture" || r.Header.Get("anthropic-version") != anthropicVersion || r.Header.Get("Authorization") != "" {
					t.Error("native request headers/path", r.URL.Path, r.Header)
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if body["model"] != "claude-sonnet-4-5-20250929" || body["max_tokens"] != float64(2048) || body["system"] != "rules\n\nmore\nrules" || body["temperature"] != 0.2 || body["top_p"] != 0.8 || !reflect.DeepEqual(body["stop_sequences"], []any{"END"}) {
					t.Error("native config", body)
				}
				messages, _ := body["messages"].([]any)
				if len(messages) != 2 {
					t.Error("history lost", messages)
				} else {
					blocks, _ := messages[1].(map[string]any)["content"].([]any)
					if len(blocks) != 3 {
						t.Error("images lost", blocks)
					} else {
						urlSource := blocks[1].(map[string]any)["source"].(map[string]any)
						dataSource := blocks[2].(map[string]any)["source"].(map[string]any)
						if urlSource["type"] != "url" || urlSource["url"] != "https://example.com/image.png" || dataSource["type"] != "base64" || dataSource["media_type"] != "image/png" || dataSource["data"] != "aGVsbG8=" {
							t.Error("native images", blocks)
						}
					}
				}
				if thinking == nil {
					if body["thinking"] != nil {
						t.Error("omitted thinking became explicit")
					}
				} else {
					value, _ := body["thinking"].(map[string]any)
					if *thinking && (value["type"] != "enabled" || value["budget_tokens"] != float64(1024)) || !*thinking && value["type"] != "disabled" {
						t.Error("thinking lost", value)
					}
				}
				fmt.Fprint(w, `{"content":[{"type":"thinking","thinking":"reason"},{"type":"text","text":"ans"},{"type":"text","text":"wer"}]}`)
			}))
			defer fixture.Close()
			urls := map[string]string{"default": "http://unused.invalid", "office": fixture.URL + "/office/"}
			d := NewAnthropicModel(urls, URLSuffix{Chat: "v1/messages"})
			urls["office"] = "http://mutated.invalid"
			limit, temp, top, stop := 2048, 0.2, 0.8, []string{"END"}
			api := &APIConfig{APIKey: stringPtr(" fixture "), Region: stringPtr("office")}
			messages := []Message{
				{Role: "system", Content: "rules"},
				{Role: "system", Content: []map[string]any{{"type": "text", "text": "more"}, {"type": "text", "text": "rules"}}},
				{Role: "assistant", Content: "old"},
				{Role: "user", Content: []any{map[string]any{"type": "text", "text": "what?"}, map[string]any{"type": "image_url", "image_url": map[string]any{"url": "https://example.com/image.png"}}, map[string]any{"type": "image_url", "image_url": map[string]any{"url": "data:image/png;base64,aGVsbG8="}}}},
			}
			answer, err := d.ChatWithMessages("claude-sonnet-4-5-20250929", api, messages, &ChatConfig{MaxTokens: &limit, Temperature: &temp, TopP: &top, Stop: &stop, Thinking: thinking})
			if err != nil || answer == nil || answer.Answer == nil || *answer.Answer != "answer" || answer.ReasoningContent == nil || *answer.ReasoningContent != "reason" || api.Context != nil {
				t.Fatal(answer, err)
			}
		})
	}
}

func TestAnthropicPreflightAndUnsupported(t *testing.T) {
	d := NewAnthropicModel(map[string]string{"default": "http://unused.invalid"}, URLSuffix{Chat: "v1/messages", Models: "v1/models"})
	var calls int
	d.httpClient.Transport = lmStudioTransportFunc(func(r *http.Request) (*http.Response, error) { calls++; return nil, errors.New("unexpected request") })
	api := &APIConfig{APIKey: stringPtr("fixture")}
	badMessages := [][]Message{
		nil, {{Role: "system", Content: "only"}}, {{Role: "tool", Content: "q"}}, {{Role: "user", Content: nil}}, {{Role: "user", Content: " "}},
		{{Role: "user", Content: []any{42}}}, {{Role: "user", Content: []any{map[string]any{"type": "text", "text": 42}}}},
		{{Role: "user", Content: []any{map[string]any{"type": "video_url"}}}},
	}
	for _, source := range []any{nil, map[string]any{}, map[string]any{"type": "url", "url": "file:///tmp/a.png"}, map[string]any{"type": "url", "url": "https://user:password@example.com/image.png"}, map[string]any{"type": "base64", "media_type": "image/png", "data": "bad"}, map[string]any{"type": "base64", "media_type": "image/png", "data": ""}, map[string]any{"type": "base64", "media_type": "text/plain", "data": "aA=="}} {
		badMessages = append(badMessages, []Message{{Role: "user", Content: []map[string]any{{"type": "image", "source": source}}}})
	}
	for _, messages := range badMessages {
		if answer, err := d.ChatWithMessages("claude", api, messages, nil); err == nil || answer != nil {
			t.Fatal("bad message succeeded", messages, answer, err)
		}
	}
	good := []Message{{Role: "user", Content: "q"}}
	for _, invalid := range []*APIConfig{nil, {}, {APIKey: stringPtr(" ")}, {APIKey: stringPtr("fixture"), Region: stringPtr("missing")}} {
		if answer, err := d.ChatWithMessages("claude", invalid, good, nil); err == nil || answer != nil {
			t.Fatal(answer, err)
		}
	}
	for _, config := range []*ChatConfig{{MaxTokens: anthropicTestPtr(0)}, {Thinking: anthropicTestPtr(true), MaxTokens: anthropicTestPtr(1024)}, {Temperature: anthropicTestPtr(math.NaN())}} {
		if answer, err := d.ChatWithMessages("claude", api, good, config); err == nil || answer != nil {
			t.Fatal(answer, err)
		}
	}
	if _, err := d.ChatWithMessages(" ", api, good, nil); err == nil {
		t.Fatal("empty model")
	}
	if err := d.ChatStreamlyWithMessages("claude", good, api, nil, func(*string, *string) error { return nil }); err == nil {
		t.Fatal("streaming")
	}
	if _, err := d.Embed(stringPtr("claude"), []string{"q"}, api, nil); err == nil {
		t.Fatal("embedding")
	}
	if _, ok := any(d).(TTSDriver); ok {
		t.Fatal("invented TTS")
	}
	if _, ok := any(d).(ASRDriver); ok {
		t.Fatal("invented ASR")
	}
	if calls != 0 {
		t.Fatal("rejected operation reached provider", calls)
	}
}

func TestAnthropicStrictResponsesHTTP(t *testing.T) {
	for _, body := range []string{``, `null`, `{}`, `{"content":[]}`, `{"content":[{"type":"text"}]}`, `{"content":[{"type":"text","text":null}]}`, `{"content":[{"type":"text","text":" "}]}`, `{"content":[{"type":"thinking","thinking":"thought"}]}`, `{"content":[{"type":"text","text":"ok"},{"type":"thinking"}]}`, `{"content":[{"type":"tool_use"}]}`, `{"content":[{"type":"text","text":"ok"}],"error":{"message":"private-body"}}`, `{"content":[{"type":"text","text":"ok"}]} {}`} {
		t.Run(body, func(t *testing.T) {
			fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
			defer fixture.Close()
			d := NewAnthropicModel(map[string]string{"default": fixture.URL}, URLSuffix{Chat: "messages"})
			answer, err := d.ChatWithMessages("claude", &APIConfig{APIKey: stringPtr("fixture")}, []Message{{Role: "user", Content: "q"}}, nil)
			if err == nil || answer != nil || strings.Contains(err.Error(), "private-body") {
				t.Fatal(answer, err)
			}
		})
	}
}

func TestAnthropicDiscoveryHTTP(t *testing.T) {
	for _, body := range []string{`{"data":[]}`, `{"data":[{"id":"claude"}]}`, `{}`, `{"data":null}`, `{"data":[{"id":""}]}`, `{"data":[{"id":"claude"},{"id":"claude"}]}`, `{"error":{"message":"private-body"}}`, `{"data":[]} {}`} {
		t.Run(body, func(t *testing.T) {
			var calls atomic.Int32
			fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.Method != "GET" || r.URL.Path != "/v1/models" || r.Header.Get("x-api-key") != "fixture" {
					t.Error("discovery request")
				}
				fmt.Fprint(w, body)
			}))
			defer fixture.Close()
			d := NewAnthropicModel(map[string]string{"default": fixture.URL}, URLSuffix{Models: "v1/models"})
			api := &APIConfig{APIKey: stringPtr("fixture")}
			result, err := d.ListModels(api)
			valid := body == `{"data":[]}` || body == `{"data":[{"id":"claude"}]}`
			if valid {
				if err != nil || result == nil {
					t.Fatal(result, err)
				}
			} else if err == nil || result != nil || strings.Contains(err.Error(), "private-body") {
				t.Fatal(result, err)
			}
			if e := d.CheckConnection(api); (e == nil) != valid || calls.Load() != 2 {
				t.Fatal(e, calls.Load())
			}
		})
	}
}

func TestAnthropicHTTPStatusAndCancellation(t *testing.T) {
	for _, status := range []int{401, 403, 429, 500} {
		fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(status); fmt.Fprint(w, "private-body") }))
		d := NewAnthropicModel(map[string]string{"default": fixture.URL}, URLSuffix{Chat: "messages"})
		answer, err := d.ChatWithMessages("claude", &APIConfig{APIKey: stringPtr("fixture")}, []Message{{Role: "user", Content: "q"}}, nil)
		fixture.Close()
		if err == nil || answer != nil || !strings.Contains(err.Error(), fmt.Sprint(status)) || strings.Contains(err.Error(), "private-body") {
			t.Fatal(answer, err)
		}
	}
	started := make(chan struct{})
	fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body any
		json.NewDecoder(r.Body).Decode(&body)
		close(started)
		<-r.Context().Done()
	}))
	defer fixture.Close()
	d := NewAnthropicModel(map[string]string{"default": fixture.URL}, URLSuffix{Chat: "messages"})
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	result := make(chan error, 1)
	go func() {
		_, err := d.ChatWithMessages("claude", &APIConfig{APIKey: stringPtr("fixture"), Context: ctx}, []Message{{Role: "user", Content: "q"}}, nil)
		result <- err
	}()
	select {
	case <-started:
		cancel()
	case <-ctx.Done():
		t.Fatal("request did not start")
	}
	if err := <-result; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}
