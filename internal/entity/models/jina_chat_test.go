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
	"sync/atomic"
	"testing"
)

func TestJinaChatHTTP(t *testing.T) {
	var calls atomic.Int32
	fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if r.Method != http.MethodPost || r.URL.Path != "/office/chat/completions" || r.Header.Get("Authorization") != "Bearer key" || r.Header.Get("Content-Type") != "application/json" || body["model"] != "jina-vlm" || body["stream"] != false || body["max_tokens"] != float64(128) || body["temperature"] != 0.2 || body["top_p"] != 0.8 || !reflect.DeepEqual(body["stop"], []any{"END"}) {
			t.Error("Jina request lost config", body, r.URL.Path)
		}
		messages, _ := body["messages"].([]any)
		if len(messages) != 3 || messages[0].(map[string]any)["role"] != "system" || messages[1].(map[string]any)["role"] != "assistant" {
			t.Error("history lost", messages)
		}
		fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
	}))
	defer fixture.Close()
	urls := map[string]string{"default": "http://unused.invalid", "office": fixture.URL + "/office/"}
	d := NewJinaModel(urls, URLSuffix{Chat: "chat/completions"})
	urls["office"] = "http://mutated.invalid"
	limit, temp, topP, stop := 128, 0.2, 0.8, []string{"END"}
	api := &APIConfig{APIKey: stringPtr(" key "), Region: stringPtr("office")}
	response, err := d.ChatWithMessages("jina-vlm", api, []Message{{Role: "system", Content: "rules"}, {Role: "assistant", Content: "old"}, {Role: "user", Content: "q"}}, &ChatConfig{MaxTokens: &limit, Temperature: &temp, TopP: &topP, Stop: &stop})
	if err != nil || response == nil || response.Answer == nil || *response.Answer != "answer" || calls.Load() != 1 || api.Context != nil {
		t.Fatal(response, err, calls.Load())
	}
	for _, streaming := range []func() error{
		func() error {
			return d.ChatStreamlyWithMessages("jina-vlm", []Message{{Role: "user", Content: "q"}}, api, nil, func(*string, *string) error { return nil })
		},
		func() error {
			return d.ChatStreamlyWithSender(stringPtr("jina-vlm"), stringPtr("q"), api, nil, func(*string, *string) error { return nil })
		},
	} {
		if err := streaming(); err == nil {
			t.Fatal("streaming falsely supported")
		}
	}
	if calls.Load() != 1 {
		t.Fatal("unsupported operation made request")
	}
}

func TestJinaChatFailuresHTTP(t *testing.T) {
	for _, response := range []string{``, `null`, `{}`, `{"choices":[]}`, `{"choices":[{}]}`, `{"choices":[{"message":{"content":null}}]}`, `{"choices":[{"message":{"content":" "}}]}`, `{"choices":[{"message":{"content":42}}]}`, `{"choices":[{"message":{"content":"ok"}}],"error":{"message":"private-body"}}`, `{"choices":[{"message":{"content":"ok"}}]} {}`} {
		t.Run(response, func(t *testing.T) {
			fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, response) }))
			defer fixture.Close()
			d := NewJinaModel(map[string]string{"default": fixture.URL}, URLSuffix{Chat: "chat"})
			answer, err := d.ChatWithMessages("jina-vlm", &APIConfig{APIKey: stringPtr("key")}, []Message{{Role: "user", Content: "q"}}, nil)
			if err == nil || answer != nil || strings.Contains(err.Error(), "private-body") {
				t.Fatal(answer, err)
			}
		})
	}
	for _, status := range []int{401, 429, 500} {
		t.Run(fmt.Sprint(status), func(t *testing.T) {
			fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(status); fmt.Fprint(w, "private-body") }))
			defer fixture.Close()
			d := NewJinaModel(map[string]string{"default": fixture.URL}, URLSuffix{Chat: "chat"})
			answer, err := d.ChatWithMessages("jina-vlm", &APIConfig{APIKey: stringPtr("key")}, []Message{{Role: "user", Content: "q"}}, nil)
			if err == nil || answer != nil || !strings.Contains(err.Error(), fmt.Sprint(status)) || strings.Contains(err.Error(), "private-body") {
				t.Fatal(answer, err)
			}
		})
	}
}

func TestJinaChatPreflightAndCancellation(t *testing.T) {
	d := NewJinaModel(map[string]string{"default": "http://unused.invalid"}, URLSuffix{Chat: "chat"})
	var calls int
	d.httpClient.Transport = lmStudioTransportFunc(func(r *http.Request) (*http.Response, error) { calls++; return nil, errors.New("unexpected request") })
	for _, api := range []*APIConfig{nil, {}, {APIKey: stringPtr(" ")}, {APIKey: stringPtr("key"), Region: stringPtr("missing")}} {
		if answer, err := d.ChatWithMessages("jina-vlm", api, []Message{{Role: "user", Content: "q"}}, nil); err == nil || answer != nil {
			t.Fatal(answer, err)
		}
	}
	api := &APIConfig{APIKey: stringPtr("key")}
	if _, err := d.ChatWithMessages(" ", api, []Message{{Role: "user", Content: "q"}}, nil); err == nil {
		t.Fatal("empty name")
	}
	if _, err := d.ChatWithMessages("jina-vlm", api, nil, nil); err == nil {
		t.Fatal("empty messages")
	}
	if calls != 0 {
		t.Fatal("invalid request reached provider")
	}
	started := make(chan struct{})
	fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body any
		json.NewDecoder(r.Body).Decode(&body)
		close(started)
		<-r.Context().Done()
	}))
	defer fixture.Close()
	d = NewJinaModel(map[string]string{"default": fixture.URL}, URLSuffix{Chat: "chat"})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	result := make(chan error, 1)
	go func() {
		_, err := d.ChatWithMessages("jina-vlm", &APIConfig{APIKey: stringPtr("key"), Context: ctx}, []Message{{Role: "user", Content: "q"}}, nil)
		result <- err
	}()
	<-started
	cancel()
	if err := <-result; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}
