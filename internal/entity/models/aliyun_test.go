package models

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func aliyunTestModel(baseURL string) *AliyunModel {
	return NewAliyunModel(
		map[string]string{"default": baseURL, "singapore": baseURL + "/intl"},
		URLSuffix{Chat: "chat/completions", Models: "models"},
	)
}

func TestAliyunChatAndMessages(t *testing.T) {
	var requests []map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/chat/completions" {
			t.Errorf("request = %s %s", r.Method, r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Bearer test-key" {
			t.Errorf("authorization = %q", r.Header.Get("Authorization"))
		}
		var body map[string]interface{}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Errorf("decode request: %v", err)
		}
		requests = append(requests, body)
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"choices":[{"message":{"content":"answer","reasoning_content":"\nreason"}}]}`)
	}))
	defer server.Close()

	model := aliyunTestModel(server.URL)
	key, name, prompt := "test-key", "qwen-flash", "hello"
	thinking, stream := true, true
	response, err := model.Chat(&name, &prompt, &APIConfig{APIKey: &key}, &ChatConfig{Thinking: &thinking, Stream: &stream})
	if err != nil || response.Answer == nil || *response.Answer != "answer" || response.ReasoningContent == nil || *response.ReasoningContent != "reason" {
		t.Fatalf("Chat() = %#v, %v", response, err)
	}
	answer, err := model.ChatWithMessages(name, &key, []Message{{Role: "system", Content: "guide"}, {Role: "user", Content: prompt}}, nil)
	if err != nil || answer != "answer" {
		t.Fatalf("ChatWithMessages() = %q, %v", answer, err)
	}
	if len(requests) != 2 || requests[0]["model"] != name || requests[0]["stream"] != false || requests[0]["enable_thinking"] != true {
		t.Fatalf("chat request = %#v", requests)
	}
	messages, ok := requests[1]["messages"].([]interface{})
	if !ok || len(messages) != 2 || messages[0].(map[string]interface{})["role"] != "system" {
		t.Fatalf("messages request = %#v", requests[1])
	}
}

func TestAliyunStreamSendsDeltasAndDone(t *testing.T) {
	largeContent := strings.Repeat("x", 128*1024)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/intl/chat/completions" {
			t.Errorf("path = %q", r.URL.Path)
		}
		var body map[string]interface{}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Errorf("decode request: %v", err)
		}
		if body["stream"] != true {
			t.Errorf("stream = %#v", body["stream"])
		}
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"thinking\"}}]}\n\n")
		fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":%q}}]}\n\n", largeContent)
		fmt.Fprint(w, "data: [DONE]\n\n")
	}))
	defer server.Close()
	model := aliyunTestModel(server.URL)
	key, name, prompt, region := "test-key", "qwen-flash", "hello", "singapore"
	var content, reasoning, done string
	err := model.ChatStreamlyWithSender(&name, &prompt, &APIConfig{APIKey: &key, Region: &region}, nil, func(answer, thought *string) error {
		if answer != nil {
			if *answer == "[DONE]" {
				done = *answer
			} else {
				content += *answer
			}
		}
		if thought != nil {
			reasoning += *thought
		}
		return nil
	})
	if err != nil || content != largeContent || reasoning != "thinking" || done != "[DONE]" {
		t.Fatalf("stream content length = %d, reasoning = %q, done = %q, err = %v", len(content), reasoning, done, err)
	}
}

func TestAliyunRejectsErrorsWithoutFalseDone(t *testing.T) {
	key, name, prompt, region := "test-key", "qwen-flash", "hello", "unknown"
	model := aliyunTestModel("https://example.invalid")
	if _, err := model.Chat(&name, &prompt, &APIConfig{APIKey: &key, Region: &region}, nil); err == nil || !strings.Contains(err.Error(), "region") {
		t.Fatalf("unknown region error = %v", err)
	}
	if _, err := model.Chat(&name, &prompt, nil, nil); err == nil || !strings.Contains(err.Error(), "API key") {
		t.Fatalf("missing API key error = %v", err)
	}

	for _, testCase := range []struct {
		name   string
		status int
		body   string
	}{
		{name: "HTTP failure", status: http.StatusUnauthorized, body: `{"error":"invalid key"}`},
		{name: "truncated stream", status: http.StatusOK, body: "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n"},
		{name: "bad event", status: http.StatusOK, body: "data: {bad}\n\n"},
		{name: "business error", status: http.StatusOK, body: "data: {\"error\":{\"message\":\"rate limit\"}}\n\n"},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(testCase.status)
				fmt.Fprint(w, testCase.body)
			}))
			defer server.Close()
			model := aliyunTestModel(server.URL)
			var received []string
			err := model.ChatStreamlyWithSender(&name, &prompt, &APIConfig{APIKey: &key}, nil, func(answer, thought *string) error {
				if answer != nil {
					received = append(received, *answer)
				}
				return nil
			})
			if err == nil {
				t.Fatal("stream returned success for an error response")
			}
			for _, item := range received {
				if item == "[DONE]" {
					t.Fatalf("false DONE after %v", err)
				}
			}
		})
	}
}

func TestAliyunChatRejectsErrorResponses(t *testing.T) {
	for _, testCase := range []struct {
		name   string
		status int
		body   string
	}{
		{name: "HTTP failure", status: http.StatusUnauthorized, body: `{"error":"invalid key"}`},
		{name: "business error", status: http.StatusOK, body: `{"error":{"message":"rate limit"}}`},
		{name: "missing answer", status: http.StatusOK, body: `{"choices":[{"message":{}}]}`},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(testCase.status)
				fmt.Fprint(w, testCase.body)
			}))
			defer server.Close()
			model := aliyunTestModel(server.URL)
			key, name, prompt := "test-key", "qwen-flash", "hello"
			if response, err := model.Chat(&name, &prompt, &APIConfig{APIKey: &key}, nil); err == nil || response != nil {
				t.Fatalf("Chat() = %#v, %v", response, err)
			}
		})
	}
}

func TestAliyunListModelsUsesConfiguredEndpoint(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/models" {
			t.Errorf("request = %s %s", r.Method, r.URL.Path)
		}
		fmt.Fprint(w, `{"data":[{"id":"qwen-flash"}]}`)
	}))
	defer server.Close()
	model := aliyunTestModel(server.URL)
	key := "test-key"
	models, err := model.ListModels(&APIConfig{APIKey: &key})
	if err != nil || len(models) != 1 || models[0] != "qwen-flash" {
		t.Fatalf("ListModels() = %#v, %v", models, err)
	}
}

func TestAliyunListModelsRejectsBusinessError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"error":{"message":"bad key"}}`)
	}))
	defer server.Close()
	model := aliyunTestModel(server.URL)
	key := "test-key"
	if err := model.CheckConnection(&APIConfig{APIKey: &key}); err == nil || !strings.Contains(err.Error(), "bad key") {
		t.Fatalf("CheckConnection() error = %v", err)
	}
}
