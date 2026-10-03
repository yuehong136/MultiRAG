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

func TestParseChatModelIdentifierAndReasoningOptions(t *testing.T) {
	tests := []struct {
		input     string
		paramName string
		want      string
	}{
		{input: `THINK CHAT "deepseek-v4-pro@default@deepseek" "hello" WITH EFFORT HIGH;`, paramName: "effort", want: "high"},
		{input: `THINK CHAT "gpt-5.2@default@openai" "hello" WITH VERBOSITY MEDIUM;`, paramName: "verbosity", want: "medium"},
	}
	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			cmd, err := NewParser(tt.input).Parse(false)
			if err != nil {
				t.Fatalf("Parse() error = %v", err)
			}
			if cmd.Params["model_name"] == nil || cmd.Params[tt.paramName] != tt.want {
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

func TestParseCheckProviderConnection(t *testing.T) {
	cmd, err := NewParser(`CHECK INSTANCE "primary" FROM "minimax";`).Parse(false)
	if err != nil {
		t.Fatalf("Parse() error = %v", err)
	}
	if cmd.Type != "check_provider_connection" || cmd.Params["provider_name"] != "minimax" || cmd.Params["instance_name"] != "primary" {
		t.Fatalf("command = %#v", cmd)
	}

	if _, err = NewParser(`CHECK INSTANCE "primary" FROM "minimax"`).Parse(false); err == nil {
		t.Fatal("Parse() error = nil, want missing semicolon error")
	}
}

func TestNonStreamChatUsesThinkingPayload(t *testing.T) {
	var body map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/chat/completions" {
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

	cmd, err := NewParser(`THINK CHAT "glm-test@default@zhipu-ai" "hello" WITH EFFORT HIGH;`).Parse(false)
	if err != nil {
		t.Fatalf("Parse() error = %v", err)
	}
	response, err := client.ExecuteUserCommand(cmd)
	if err != nil {
		t.Fatalf("ExecuteUserCommand() error = %v", err)
	}
	if body["provider_name"] != "zhipu-ai" || body["instance_name"] != "default" || body["stream"] != false || body["thinking"] != true || body["model_name"] != "glm-test" || body["effort"] != "high" {
		t.Fatalf("body = %#v", body)
	}
	result, ok := response.(*NonStreamResponse)
	if !ok || result.Answer != "answer" || result.ReasoningContent != "reason" {
		t.Fatalf("response = %#v", response)
	}
}

func TestStreamChatCompletionAndFailure(t *testing.T) {
	for _, test := range []struct {
		name, body string
		fail       bool
	}{
		{"complete", "event: message\ndata: [REASONING]reason\n\nevent: message\ndata: [MESSAGE]answer\n\nevent: done\ndata: [DONE]\n\n", false},
		{"provider error", "event: error\ndata: Model stream failed\n\n", true},
		{"partial", "event: message\ndata: [MESSAGE]partial\n\n", true},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/api/v1/chat/completions" || r.Header.Get("Authorization") != "fixture-login" {
					t.Errorf("route/auth = %s %q", r.URL.Path, r.Header.Get("Authorization"))
				}
				var body map[string]interface{}
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if body["provider_name"] != "Moonshot" || body["instance_name"] != "default" || body["model_name"] != "kimi-k2.5" || body["stream"] != true || body["thinking"] != false {
					t.Errorf("body = %#v", body)
				}
				w.Header().Set("Content-Type", "text/event-stream")
				_, _ = w.Write([]byte(test.body))
			}))
			defer server.Close()
			u, _ := url.Parse(server.URL)
			port, _ := strconv.Atoi(u.Port())
			client := NewMultiRAGClient("user")
			client.HTTPClient.Host = u.Hostname()
			client.HTTPClient.Port = port
			client.HTTPClient.LoginToken = "fixture-login"
			client.CurrentModel = &CurrentModel{Provider: "Moonshot", Instance: "default", Model: "kimi-k2.5"}
			cmd, err := NewParser(`STREAM CHAT "hello";`).Parse(false)
			if err != nil {
				t.Fatal(err)
			}
			result, err := client.ExecuteUserCommand(cmd)
			if test.fail {
				if err == nil {
					t.Fatalf("false success = %#v", result)
				}
			} else if err != nil {
				t.Fatal(err)
			} else if result.(*StreamMessageResponse).Message != "answer" {
				t.Fatalf("answer = %#v", result)
			}
		})
	}
}
