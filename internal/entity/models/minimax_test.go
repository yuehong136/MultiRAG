package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestMinimaxChatRequestModesAndConfiguration(t *testing.T) {
	for _, thinkingConfig := range []struct {
		name  string
		value *bool
	}{{"default", nil}, {"enabled", minimaxPtr(true)}, {"disabled", minimaxPtr(false)}} {
		thinking := thinkingConfig.value
		for _, stream := range []bool{false, true} {
			t.Run(fmt.Sprintf("thinking=%s/stream=%v", thinkingConfig.name, stream), func(t *testing.T) {
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if r.Method != http.MethodPost || r.URL.Path != "/api/custom/chat" || r.Header.Get("Authorization") != "Bearer key" || r.Header.Get("Content-Type") != "application/json" {
						t.Errorf("incorrect request: %s %s", r.Method, r.URL.Path)
					}
					accept := "application/json"
					if stream {
						accept = "text/event-stream"
					}
					if r.Header.Get("Accept") != accept {
						t.Errorf("Accept = %q", r.Header.Get("Accept"))
					}
					var body map[string]interface{}
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
						return
					}
					want := map[string]interface{}{
						"model": "minimax-m2.7", "stream": stream,
						"messages":   []interface{}{map[string]interface{}{"role": "user", "content": "question"}},
						"max_tokens": float64(123), "temperature": 0.5, "top_p": 0.8,
						"do_sample": false, "stop": []interface{}{"stop"},
					}
					if thinking != nil {
						mode := "disabled"
						if *thinking {
							mode = "adaptive"
							want["reasoning_split"] = true
						}
						want["thinking"] = map[string]interface{}{"type": mode}
					}
					if !reflect.DeepEqual(body, want) {
						t.Errorf("body = %#v; want %#v", body, want)
					}
					if stream {
						fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\",\"reasoning_content\":\"reason\"}}]}\n\ndata: [DONE]\n\n")
					} else {
						fmt.Fprint(w, `{"choices":[{"message":{"content":"answer","reasoning_content":"\nreason"}}]}`)
					}
				}))
				defer server.Close()
				driver, err := NewModelFactory().CreateModelDriver("MiNiMaX", map[string]string{"default": "http://invalid.invalid", "fixture": server.URL + "/api/"}, URLSuffix{Chat: "/custom/chat"})
				if err != nil || driver.Name() != "minimax" {
					t.Fatalf("driver = %v, %v", driver, err)
				}
				key, name, message, region := " key ", "minimax-m2.7", "question", "fixture"
				apiConfig := &APIConfig{APIKey: &key, Region: &region}
				config := &ChatConfig{Thinking: thinking, Stream: minimaxPtr(!stream), MaxTokens: minimaxPtr(123), Temperature: minimaxPtr(0.5), TopP: minimaxPtr(0.8), DoSample: minimaxPtr(false), Stop: &[]string{"stop"}}
				if stream {
					var frames []string
					err := driver.ChatStreamlyWithSender(&name, &message, apiConfig, config, minimaxFrameCollector(&frames))
					if err != nil || !reflect.DeepEqual(frames, []string{"reason:reason", "answer", "[DONE]"}) {
						t.Fatalf("frames = %v, %v", frames, err)
					}
				} else {
					response, err := driver.Chat(&name, &message, apiConfig, config)
					if err != nil {
						t.Fatal(err)
					}
					assertStringPointer(t, response.Answer, stringPtr("answer"))
					assertStringPointer(t, response.ReasoningContent, stringPtr("reason"))
				}
			})
		}
	}
}

func minimaxPtr[T any](value T) *T { return &value }

func minimaxFrameCollector(frames *[]string) func(*string, *string) error {
	return func(content, reasoning *string) error {
		if reasoning != nil {
			*frames = append(*frames, "reason:"+*reasoning)
		}
		if content != nil {
			*frames = append(*frames, *content)
		}
		return nil
	}
}

func TestMinimaxHistoryChat(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Messages []Message
			Stream   bool
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		want := []Message{{Role: "system", Content: "instruction"}, {Role: "user", Content: "first"}, {Role: "assistant", Content: "previous answer"}, {Role: "user", Content: "follow up"}}
		if body.Stream || !reflect.DeepEqual(body.Messages, want) {
			t.Errorf("history = %#v", body)
		}
		fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
	}))
	defer server.Close()
	driver := NewMinimaxModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
	key, name := "key", "minimax-m2.7"
	model := NewChatModel(driver, &name, &APIConfig{APIKey: &key})
	history := []map[string]string{{"role": "user", "content": "first"}, {"role": "assistant", "content": "previous answer"}, {"role": "user", "content": "follow up"}}
	answer, err := model.Chat("instruction", history, nil)
	if err != nil || answer != "answer" {
		t.Fatalf("answer = %q, %v", answer, err)
	}
}

func TestMinimaxChatResponses(t *testing.T) {
	for _, test := range []struct {
		name, body string
		status     int
		wantErr    bool
		reasoning  *string
	}{
		{"plain answer", `{"choices":[{"message":{"content":"answer"}}]}`, 200, false, nil},
		{"null reasoning", `{"error":null,"choices":[{"message":{"content":"answer","reasoning_content":null}}]}`, 200, false, nil},
		{"reasoning", `{"choices":[{"message":{"content":"answer","reasoning_content":"\nreason"}}]}`, 200, false, stringPtr("reason")},
		{"HTTP error", `secret-key-and-prompt`, 401, true, nil},
		{"base response error", `{"base_resp":{"status_code":1002,"status_msg":"secret-key-and-prompt"},"choices":[{"message":{"content":"answer"}}]}`, 200, true, nil},
		{"business error", `{"error":{"message":"secret-key-and-prompt"},"choices":[{"message":{"content":"answer"}}]}`, 200, true, nil},
		{"bad JSON", `{`, 200, true, nil},
		{"no choices", `{"choices":[]}`, 200, true, nil},
		{"no message", `{"choices":[{}]}`, 200, true, nil},
		{"null content", `{"choices":[{"message":{"content":null}}]}`, 200, true, nil},
		{"empty content", `{"choices":[{"message":{"content":""}}]}`, 200, true, nil},
		{"invalid content", `{"choices":[{"message":{"content":42}}]}`, 200, true, nil},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(test.status)
				fmt.Fprint(w, test.body)
			}))
			defer server.Close()
			driver := NewMinimaxModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
			key, name, message := "key", "minimax-m2.7", "question"
			response, err := driver.Chat(&name, &message, &APIConfig{APIKey: &key}, &ChatConfig{Thinking: minimaxPtr(true)})
			if test.wantErr {
				if err == nil || response != nil || strings.Contains(err.Error(), "secret-key-and-prompt") {
					t.Fatalf("response = %#v, %v", response, err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			assertStringPointer(t, response.Answer, stringPtr("answer"))
			assertStringPointer(t, response.ReasoningContent, test.reasoning)
		})
	}
}

func TestMinimaxStreamContracts(t *testing.T) {
	answerEvent := "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"},\"finish_reason\":\"stop\"}]}\n\n"
	for _, test := range []struct {
		name, body string
		wantFrames []string
		wantErr    bool
	}{
		{"combined delta", ": heartbeat\n\ndata:\n\ndata: {\"choices\":[{\"delta\":{\"role\":\"assistant\"}}]}\n\ndata: {\"choices\":[{\"delta\":{\"reasoning_content\":\"reason\",\"content\":\"answer\"}}]}\n\ndata: {\"choices\":[],\"usage\":{}}\n\ndata: [DONE]\n\n", []string{"reason:reason", "answer", "[DONE]"}, false},
		{"finish then done", answerEvent + "data: [DONE]\n\ndata: [DONE]\n\n", []string{"answer", "[DONE]"}, false},
		{"empty stream", "data: [DONE]\n\n", nil, true},
		{"reasoning only", "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"reason\",\"content\":null}}]}\n\ndata: [DONE]\n\n", []string{"reason:reason"}, true},
		{"truncated stream", answerEvent, []string{"answer"}, true},
		{"base response stream error", answerEvent + "data: {\"base_resp\":{\"status_code\":1002,\"status_msg\":\"secret-key-and-prompt\"}}\n\ndata: [DONE]\n\n", []string{"answer"}, true},
		{"bad JSON", answerEvent + "data: nope\n\n", []string{"answer"}, true},
		{"business error after answer", answerEvent + "data: {\"error\":{\"message\":\"secret-key-and-prompt\"}}\n\ndata: [DONE]\n\n", []string{"answer"}, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, test.body) }))
			defer server.Close()
			driver := NewMinimaxModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
			key, name, message := "key", "minimax-m2.7", "question"
			var frames []string
			err := driver.ChatStreamlyWithSender(&name, &message, &APIConfig{APIKey: &key}, nil, minimaxFrameCollector(&frames))
			if (err != nil) != test.wantErr || !reflect.DeepEqual(frames, test.wantFrames) || (err != nil && strings.Contains(err.Error(), "secret-key-and-prompt")) {
				t.Fatalf("frames = %v, error = %v", frames, err)
			}
			if test.name == "truncated stream" && !errors.Is(err, io.ErrUnexpectedEOF) {
				t.Fatalf("truncated stream error = %v", err)
			}
		})
	}
}

func TestMinimaxLargeStreamDelta(t *testing.T) {
	answer := strings.Repeat("a", 100000)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":%q}}]}\n\ndata: [DONE]\n\n", answer)
	}))
	defer server.Close()
	driver := NewMinimaxModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
	key, name, message := "key", "minimax-m2.7", "question"
	var frames []string
	err := driver.ChatStreamlyWithSender(&name, &message, &APIConfig{APIKey: &key}, nil, minimaxFrameCollector(&frames))
	if err != nil || !reflect.DeepEqual(frames, []string{answer, "[DONE]"}) {
		t.Fatalf("large delta: frames=%d, error=%v", len(frames), err)
	}
}

func TestMinimaxSenderErrors(t *testing.T) {
	for _, failAt := range []int{1, 2, 3} {
		t.Run(fmt.Sprint(failAt), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"reason\",\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
			}))
			defer server.Close()
			driver := NewMinimaxModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
			key, name, message := "key", "minimax-m2.7", "question"
			failure, calls := errors.New("sender failed"), 0
			err := driver.ChatStreamlyWithSender(&name, &message, &APIConfig{APIKey: &key}, nil, func(*string, *string) error {
				calls++
				if calls == failAt {
					return failure
				}
				return nil
			})
			if err != failure || calls != failAt {
				t.Fatalf("sender error = %v, calls = %d", err, calls)
			}
		})
	}
}

func TestMinimaxCancellationReachesTransport(t *testing.T) {
	for _, stream := range []bool{false, true} {
		t.Run(fmt.Sprint(stream), func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			started, canceled := make(chan struct{}), make(chan struct{})
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if _, err := io.Copy(io.Discard, r.Body); err != nil {
					t.Error(err)
				}
				if stream {
					fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\n")
				} else {
					fmt.Fprint(w, `{"choices":[`)
				}
				w.(http.Flusher).Flush()
				close(started)
				<-r.Context().Done()
				close(canceled)
			}))
			defer server.Close()
			defer cancel()
			driver := NewMinimaxModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
			key, name, message := "key", "minimax-m2.7", "question"
			apiConfig := &APIConfig{APIKey: &key, Context: ctx}
			result := make(chan error, 1)
			var frames []string
			delivered := make(chan struct{}, 1)
			go func() {
				if stream {
					result <- driver.ChatStreamlyWithSender(&name, &message, apiConfig, nil, func(content, reason *string) error {
						minimaxFrameCollector(&frames)(content, reason)
						delivered <- struct{}{}
						return nil
					})
				} else {
					_, err := driver.Chat(&name, &message, apiConfig, nil)
					result <- err
				}
			}()
			select {
			case <-started:
			case <-time.After(3 * time.Second):
				t.Fatal("provider request did not start")
			}
			if stream {
				select {
				case <-delivered:
				case <-time.After(3 * time.Second):
					t.Fatal("first delta was not delivered")
				}
			}
			cancel()
			select {
			case err := <-result:
				if !errors.Is(err, context.Canceled) {
					t.Fatalf("cancellation error = %v", err)
				}
				if stream && !reflect.DeepEqual(frames, []string{"answer"}) {
					t.Fatalf("cancel produced unexpected frames: %v", frames)
				}
			case <-time.After(3 * time.Second):
				t.Fatal("client did not stop")
			}
			select {
			case <-canceled:
			case <-time.After(3 * time.Second):
				t.Fatal("provider request was not canceled")
			}
		})
	}
}

func TestMinimaxChatValidation(t *testing.T) {
	driver := NewMinimaxModel(map[string]string{"default": "http://127.0.0.1:1"}, URLSuffix{Chat: "chat"})
	key, name, message := "key", "minimax-m2.7", "question"
	for _, config := range []*APIConfig{nil, {}, {APIKey: minimaxPtr("")}, {APIKey: minimaxPtr(" ")}, {APIKey: &key, Region: minimaxPtr("unknown")}} {
		if _, err := driver.Chat(&name, &message, config, nil); err == nil {
			t.Fatal("invalid API config accepted")
		}
	}
	for _, test := range []struct{ name, message *string }{{nil, &message}, {&name, nil}, {minimaxPtr(" "), &message}} {
		if _, err := driver.Chat(test.name, test.message, &APIConfig{APIKey: &key}, nil); err == nil {
			t.Fatal("invalid chat arguments accepted")
		}
	}
	if err := driver.ChatStreamlyWithSender(&name, &message, &APIConfig{APIKey: &key}, nil, nil); err == nil {
		t.Fatal("nil sender accepted")
	}
	for _, messages := range [][]Message{nil, {{Content: "no role"}}} {
		if _, err := driver.ChatWithMessages(name, &APIConfig{APIKey: &key}, messages, nil); err == nil {
			t.Fatal("invalid history accepted")
		}
	}
	driver.URLSuffix.Chat = ""
	if _, err := driver.Chat(&name, &message, &APIConfig{APIKey: &key}, nil); err == nil {
		t.Fatal("missing chat endpoint accepted")
	}
	driver.URLSuffix.Chat = "chat"
	if _, err := driver.Chat(&name, &message, &APIConfig{APIKey: &key}, &ChatConfig{Temperature: minimaxPtr(math.NaN())}); err == nil {
		t.Fatal("invalid JSON number accepted")
	}
}

func TestMinimaxHistoryStream(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Messages []Message
			Stream   bool
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		want := []Message{{Role: "system", Content: "instruction"}, {Role: "user", Content: "first"}, {Role: "assistant", Content: "previous answer"}, {Role: "user", Content: "follow up"}}
		if !body.Stream || !reflect.DeepEqual(body.Messages, want) {
			t.Errorf("history = %#v", body)
		}
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
	}))
	defer server.Close()
	driver := NewMinimaxModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
	key, name := "key", "minimax-m2.7"
	model := NewChatModel(driver, &name, &APIConfig{APIKey: &key})
	history := []map[string]string{{"role": "user", "content": "first"}, {"role": "assistant", "content": "previous answer"}, {"role": "user", "content": "follow up"}}
	var frames []string
	err := model.ChatStreamlyWithSender("instruction", history, nil, minimaxFrameCollector(&frames))
	if err != nil || !reflect.DeepEqual(frames, []string{"answer", "[DONE]"}) {
		t.Fatalf("frames = %v, %v", frames, err)
	}
}

func TestMinimaxListModels(t *testing.T) {
	for _, body := range []string{`{"data":[{"id":"minimax-m2.7"}]}`, `{"data":[]}`, `{"data":[{}]}`, `{"base_resp":{"status_code":1002}}`, `{"data":null}`, `{"data":[{"id":1}]}`} {
		t.Run(body, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				data, _ := io.ReadAll(r.Body)
				if r.Method != "GET" || r.URL.Path != "/v1/models" || len(data) != 0 || r.Header.Get("Authorization") != "Bearer key" {
					t.Errorf("invalid list request")
				}
				fmt.Fprint(w, body)
			}))
			defer server.Close()
			driver := NewMinimaxModel(map[string]string{"default": server.URL}, URLSuffix{Models: "v1/models"})
			names, err := driver.ListModels(&APIConfig{APIKey: minimaxPtr("key")})
			valid := body == `{"data":[{"id":"minimax-m2.7"}]}` || body == `{"data":[]}`
			if (err == nil) != valid {
				t.Fatalf("names = %v, err = %v", names, err)
			}
			if valid && strings.Contains(body, "minimax-m2.7") && !reflect.DeepEqual(names, []string{"minimax-m2.7"}) {
				t.Fatal(names)
			}
		})
	}
}
