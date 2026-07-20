package cli

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"testing"
)

func TestParseChatModes(t *testing.T) {
	tests := []struct {
		input    string
		thinking bool
		stream   bool
	}{
		{input: `CHAT "hello";`},
		{input: `THINK CHAT "hello";`, thinking: true},
		{input: `STREAM CHAT "hello";`, stream: true},
		{input: `STREAM THINK CHAT "hello";`, thinking: true, stream: true},
	}
	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			cmd, err := NewParser(tt.input).Parse(false)
			if err != nil {
				t.Fatalf("Parse() error = %v", err)
			}
			if cmd.Type != "chat_to_model" || cmd.Params["thinking"] != tt.thinking || cmd.Params["stream"] != tt.stream {
				t.Fatalf("command = %#v", cmd)
			}
		})
	}
}

func TestParseListSupportedModels(t *testing.T) {
	cmd, err := NewParser(`LIST SUPPORTED MODELS FROM "deepseek" "default";`).Parse(false)
	if err != nil {
		t.Fatalf("Parse() error = %v", err)
	}
	if cmd.Type != "list_supported_models" || cmd.Params["provider_name"] != "deepseek" || cmd.Params["instance_name"] != "default" {
		t.Fatalf("command = %#v", cmd)
	}
}

func TestNonStreamChatUsesThinkingPayload(t *testing.T) {
	var body map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/providers/zhipu-ai/instances/default/models/glm-test" {
			t.Errorf("path = %q", r.URL.Path)
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Errorf("decode body: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"code":0,"reasoning_content":"reason","answer":"answer"}`))
	}))
	defer server.Close()

	serverURL, _ := url.Parse(server.URL)
	port, _ := strconv.Atoi(serverURL.Port())
	client := NewMultiRAGClient("user")
	client.HTTPClient.Host = serverURL.Hostname()
	client.HTTPClient.Port = port

	cmd, err := NewParser(`THINK CHAT "zhipu-ai/default/glm-test" "hello";`).Parse(false)
	if err != nil {
		t.Fatalf("Parse() error = %v", err)
	}
	response, err := client.ExecuteUserCommand(cmd)
	if err != nil {
		t.Fatalf("ExecuteUserCommand() error = %v", err)
	}
	if body["stream"] != false || body["thinking"] != true {
		t.Fatalf("body = %#v", body)
	}
	result, ok := response.(*NonStreamResponse)
	if !ok || result.Answer != "answer" || result.ReasoningContent != "reason" {
		t.Fatalf("response = %#v", response)
	}
}
