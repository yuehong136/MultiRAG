package models

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestZhipuAIChatUsesConfiguredRegionURL(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/chat/completions" {
			t.Errorf("request path = %q", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"choices":[{"message":{"content":"regional response"}}]}`)
	}))
	t.Cleanup(server.Close)

	model := NewZhipuAIModel(
		map[string]string{
			"default":  "http://127.0.0.1:1",
			"cn-north": server.URL,
		},
		URLSuffix{Chat: "chat/completions"},
	)
	region := "cn-north"
	modelName := "glm-test"
	apiKey := "test-key"
	message := "hello"

	response, err := model.Chat(
		&modelName,
		&message,
		&APIConfig{APIKey: &apiKey, Region: &region},
		&ChatConfig{},
	)
	if err != nil {
		t.Fatalf("Chat() error = %v", err)
	}
	if response.Answer == nil || *response.Answer != "regional response" {
		t.Fatalf("Chat() response = %#v", response)
	}
}

func TestZhipuAIChatReturnsThinkingContent(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]interface{}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Errorf("decode body: %v", err)
		}
		if thinking, ok := body["thinking"].(map[string]interface{}); !ok || thinking["type"] != "enabled" {
			t.Errorf("thinking = %#v", body["thinking"])
		}
		fmt.Fprint(w, `{"choices":[{"message":{"content":"answer","reasoning_content":"\nreason"}}]}`)
	}))
	t.Cleanup(server.Close)

	apiKey, modelName, message, thinking := "key", "glm-test", "hello", true
	model := NewZhipuAIModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat/completions"})
	response, err := model.Chat(&modelName, &message, &APIConfig{APIKey: &apiKey}, &ChatConfig{Thinking: &thinking})
	if err != nil {
		t.Fatalf("Chat() error = %v", err)
	}
	if response.Answer == nil || *response.Answer != "answer" || response.ReasoningContent == nil || *response.ReasoningContent != "reason" {
		t.Fatalf("response = %#v", response)
	}
}

func TestZhipuAIEmbeddingUsesConfiguredRegionURL(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/embeddings" {
			t.Errorf("request path = %q", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"data":[{"embedding":[1.5,2.5]}]}`)
	}))
	t.Cleanup(server.Close)

	model := NewZhipuAIModel(
		map[string]string{
			"default": "http://127.0.0.1:1",
			"global":  server.URL,
		},
		URLSuffix{Embedding: "embeddings"},
	)
	region := "global"
	modelName := "embedding-test"
	apiKey := "test-key"

	embeddings, err := model.EncodeToEmbedding(
		&modelName,
		[]string{"hello"},
		&APIConfig{APIKey: &apiKey, Region: &region},
		&EmbeddingConfig{},
	)
	if err != nil {
		t.Fatalf("EncodeToEmbedding() error = %v", err)
	}
	if len(embeddings) != 1 || len(embeddings[0]) != 2 || embeddings[0][0] != 1.5 {
		t.Fatalf("EncodeToEmbedding() = %#v", embeddings)
	}
}

func TestZhipuAIStreamUsesConfiguredRegionURL(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/chat/completions" {
			t.Errorf("request path = %q", r.URL.Path)
		}
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"regional chunk\"},\"finish_reason\":\"stop\"}]}\n\n")
	}))
	t.Cleanup(server.Close)

	model := NewZhipuAIModel(
		map[string]string{
			"default": "http://127.0.0.1:1",
			"global":  server.URL,
		},
		URLSuffix{Chat: "chat/completions"},
	)
	region := "global"
	stream := true
	modelName := "glm-test"
	apiKey := "test-key"
	message := "hello"
	var chunks []string
	sender := func(content, _ *string) error {
		if content != nil {
			chunks = append(chunks, *content)
		}
		return nil
	}

	err := model.ChatStreamlyWithSender(
		&modelName,
		&message,
		&APIConfig{APIKey: &apiKey, Region: &region},
		&ChatConfig{Stream: &stream},
		sender,
	)
	if err != nil {
		t.Fatalf("ChatStreamlyWithSender() error = %v", err)
	}
	if len(chunks) != 2 || chunks[0] != "regional chunk" || chunks[1] != "[DONE]" {
		t.Fatalf("stream chunks = %#v", chunks)
	}
}

func TestZhipuAIRejectsUnknownRegion(t *testing.T) {
	model := NewZhipuAIModel(
		map[string]string{"default": "https://default.example"},
		URLSuffix{Chat: "chat/completions"},
	)
	region := "missing"
	modelName := "glm-test"
	apiKey := "test-key"
	message := "hello"

	_, err := model.Chat(
		&modelName,
		&message,
		&APIConfig{APIKey: &apiKey, Region: &region},
		&ChatConfig{},
	)
	if err == nil || !strings.Contains(err.Error(), `region "missing"`) {
		t.Fatalf("Chat() error = %v", err)
	}
}
