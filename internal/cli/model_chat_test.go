package cli

import (
	"encoding/json"
	"fmt"
	"net/http"
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
		{input: `CHAT MESSAGE "hello";`},
		{input: `THINK CHAT MESSAGE "hello";`, thinking: true},
		{input: `STREAM CHAT MESSAGE "hello";`, stream: true},
		{input: `STREAM THINK CHAT MESSAGE "hello";`, thinking: true, stream: true},
	}
	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			cmd, err := NewParser(tt.input).Parse(APIMode)
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
		{input: `THINK CHAT WITH "deepseek-v4-pro@default@deepseek" MESSAGE "hello" EFFORT HIGH;`, paramName: "effort", want: "high"},
		{input: `THINK CHAT WITH "gpt-5.2@default@openai" MESSAGE "hello" VERBOSITY MEDIUM;`, paramName: "verbosity", want: "medium"},
	}
	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			cmd, err := NewParser(tt.input).Parse(APIMode)
			if err != nil {
				t.Fatalf("Parse() error = %v", err)
			}
			if cmd.Params["composite_model_name"] == nil || cmd.Params[tt.paramName] != tt.want {
				t.Fatalf("command = %#v", cmd)
			}
		})
	}
}

func TestParseListSupportedModels(t *testing.T) {
	cmd, err := NewParser(`LIST SUPPORTED MODELS FROM "deepseek" "default";`).Parse(APIMode)
	if err != nil {
		t.Fatalf("Parse() error = %v", err)
	}
	if cmd.Type != "list_supported_models" || cmd.Params["provider_name"] != "deepseek" || cmd.Params["instance_name"] != "default" {
		t.Fatalf("command = %#v", cmd)
	}
}

func TestParseCheckProviderConnection(t *testing.T) {
	cmd, err := NewParser(`CHECK INSTANCE "primary" FROM "minimax";`).Parse(APIMode)
	if err != nil {
		t.Fatalf("Parse() error = %v", err)
	}
	if cmd.Type != "check_provider_connection" || cmd.Params["provider_name"] != "minimax" || cmd.Params["instance_name"] != "primary" {
		t.Fatalf("command = %#v", cmd)
	}

	if _, err = NewParser(`CHECK INSTANCE "primary" FROM "minimax"`).Parse(APIMode); err != nil {
		t.Fatal(err)
	}
}

func TestNonStreamChatUsesThinkingPayload(t *testing.T) {
	var body map[string]interface{}
	server := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
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
	client := newConsumerCLI("user")
	consumerHTTP(client).Host = serverURL.Hostname()
	consumerHTTP(client).Port = port

	cmd, err := NewParser(`THINK CHAT WITH "glm-test@default@zhipu-ai" MESSAGE "hello" EFFORT HIGH;`).Parse(APIMode)
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
	if _, present := body["message"]; present {
		t.Fatal("legacy message still emitted")
	}
	messages, ok := body["messages"].([]interface{})
	if !ok || len(messages) != 1 || messages[0].(map[string]interface{})["content"].([]interface{})[0].(map[string]interface{})["text"] != "hello" {
		t.Fatalf("messages %#v", body)
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
		{"partial", "event: message\ndata: [MESSAGE]partial\n\n", false},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
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
			client := newConsumerCLI("user")
			consumerHTTP(client).Host = u.Hostname()
			consumerHTTP(client).Port = port
			consumerHTTP(client).LoginToken = consumerTokenPtr("fixture-login")
			client.CurrentModel = &CurrentModel{Provider: "Moonshot", Instance: "default", Model: "kimi-k2.5"}
			cmd, err := NewParser(`STREAM CHAT MESSAGE "hello";`).Parse(APIMode)
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
			} else if result.(*StreamMessageResponse).Message != map[string]string{"complete": "answer", "partial": "partial"}[test.name] {
				t.Fatalf("answer = %#v", result)
			}
		})
	}
}

func TestChatCLIContentArray(t *testing.T) {
	client := skillTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Messages []struct {
				Role    string
				Content []map[string]interface{}
			}
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if len(body.Messages) != 1 || len(body.Messages[0].Content) != 2 || body.Messages[0].Content[0]["text"] != "[todo] explain this" || body.Messages[0].Content[1]["type"] != "image_url" {
			t.Fatal("multimodal content lost", body)
		}
		fmt.Fprint(w, `{"code":0,"answer":"image answer"}`)
	})
	c := consumerCLIWithHTTP(client)
	cmd, err := NewParser(`CHAT WITH 'vision@fixture@vllm' MESSAGE '[todo] explain this' IMAGE 'https://example.com/a.png';`).Parse(APIMode)
	if err != nil {
		t.Fatal(err)
	}
	result, err := c.ChatToModel(cmd)
	if err != nil || result.(*NonStreamResponse).Answer != "image answer" {
		t.Fatal(result, err)
	}
	cmd.Params["messages"] = []interface{}{42}
	if result, err := c.ChatToModel(cmd); err == nil || result != nil {
		t.Fatal("untyped message accepted")
	}
}

func TestChatUsesAPITokenWithoutLogin(t *testing.T) {
	for _, stream := range []bool{false, true} {
		srv := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Header.Get("Authorization") != "Bearer fixture-api" {
				t.Error("API token missing")
			}
			if stream {
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprint(w, "event: message\ndata: [MESSAGE]answer\n\nevent: done\ndata: [DONE]\n\n")
			} else {
				fmt.Fprint(w, `{"code":0,"answer":"answer"}`)
			}
		}))
		u, _ := url.Parse(srv.URL)
		port, _ := strconv.Atoi(u.Port())
		client := newConsumerCLI("user")
		consumerHTTP(client).Host = u.Hostname()
		consumerHTTP(client).Port = port
		consumerHTTP(client).APIToken = consumerTokenPtr("fixture-api")
		input := `CHAT WITH "model@instance@LocalAI" MESSAGE "hello";`
		if stream {
			input = "STREAM " + input
		}
		cmd, err := NewParser(input).Parse(APIMode)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := client.ExecuteUserCommand(cmd); err != nil {
			t.Fatal(err)
		}
		srv.Close()
	}
}
