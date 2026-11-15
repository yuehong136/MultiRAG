package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"

	"reflect"
	"strings"
	"testing"
)

func TestVolcEngineChatAndDiscovery(t *testing.T) {
	server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer key" {
			t.Error("incorrect credential")
		}
		switch r.URL.Path {
		case "/api/chat":
			var body map[string]interface{}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Error(err)
			}
			if body["stream"] != false || body["reasoning_effort"] != "medium" || body["thinking"].(map[string]interface{})["type"] != "enabled" {
				t.Errorf("body = %#v", body)
			}
			fmt.Fprint(w, `{"choices":[{"message":{"content":"answer","reasoning_content":"reason"}}]}`)
		case "/api/models":
			fmt.Fprint(w, `{"data":[{"id":"doubao"}]}`)
		case "/api/files":
			fmt.Fprint(w, `{"data":[]}`)
		default:
			t.Errorf("wrong path %s", r.URL.Path)
			w.WriteHeader(404)
		}
	}))
	defer server.Close()
	driver, err := NewModelFactory().CreateModelDriver("VolcEngine", map[string]string{"default": "http://invalid.invalid", "fixture": server.URL + "/api"}, URLSuffix{Chat: "chat", Models: "models", Files: "files"})
	if err != nil {
		t.Fatal(err)
	}
	key, name, message, region, thinking := "key", "doubao", "question", "fixture", true
	config := &APIConfig{ApiKey: &key, Region: &region}
	response, err := driver.ChatWithMessages(name, []Message{{Role: "user", Content: message}}, config, &ChatConfig{Thinking: &thinking})
	if err != nil || response == nil || *response.Answer != "answer" || *response.ReasonContent != "reason" {
		t.Fatalf("response = %#v, %v", response, err)
	}
	names, err := driver.ListModels(config)
	if err != nil || !reflect.DeepEqual(modelNamesForTest(names), []string{"doubao"}) {
		t.Fatalf("models = %v, %v", names, err)
	}
	if err := driver.CheckConnection(config); err != nil {
		t.Fatal(err)
	}
	if _, err := embeddingVectorsForTest(driver.Embed(&name, []string{"q"}, config, nil)); err == nil {
		t.Fatal("unsupported embedding succeeded")
	}
}

func TestVolcEngineStreamContracts(t *testing.T) {
	tests := []struct {
		name, response string
		wantErr        bool
	}{
		{"combined delta", "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"reason\",\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n", false},
		{"nil config", "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n", false},
		{"empty stream", "data: [DONE]\n\n", true},
		{"truncated stream", "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\n", true},
		{"bad JSON", "data: nope\n\n", true},
		{"business error", "data: {\"error\":{\"message\":\"secret\"}}\n\n", true},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body map[string]interface{}
				json.NewDecoder(r.Body).Decode(&body)
				if body["stream"] != true {
					t.Error("stream flag lost")
				}
				fmt.Fprint(w, test.response)
			}))
			defer server.Close()
			driver := NewVolcEngine(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
			key, name, message := "key", "doubao", "question"
			var frames []string
			err := driver.ChatStreamlyWithSender(name, []Message{{Role: "user", Content: message}}, &APIConfig{ApiKey: &key}, nil, func(content, reason *string) error {
				if reason != nil {
					frames = append(frames, "reason:"+*reason)
				}
				if content != nil {
					frames = append(frames, *content)
				}
				return nil
			})
			if (err != nil) != test.wantErr {
				t.Fatalf("error = %v", err)
			}
			if test.name == "combined delta" && !reflect.DeepEqual(frames, []string{"answer", "reason:reason", "[DONE]"}) {
				t.Fatalf("frames = %v", frames)
			}
			if test.wantErr && len(frames) > 0 && frames[len(frames)-1] == "[DONE]" {
				t.Fatal("error produced completion frame")
			}
		})
	}
}

func TestVolcEngineValidationSenderErrorAndCancellation(t *testing.T) {
	server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
	}))
	defer server.Close()
	driver := NewVolcEngine(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
	key, name, message := "key", "doubao", "question"
	failure := errors.New("sender failed")
	err := driver.ChatStreamlyWithSender(name, []Message{{Role: "user", Content: message}}, &APIConfig{ApiKey: &key}, nil, func(*string, *string) error { return failure })
	if !errors.Is(err, failure) {
		t.Fatalf("error = %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	err = driver.ChatStreamlyWithSender(name, []Message{{Role: "user", Content: message}}, &APIConfig{ApiKey: &key, Context: ctx}, nil, func(*string, *string) error { return nil })
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("cancel error = %v", err)
	}
	if _, err := driver.ChatWithMessages("", []Message{{Role: "user", Content: message}}, &APIConfig{ApiKey: &key}, nil); err == nil {
		t.Fatal("empty model name accepted")
	}
	if _, err := driver.ChatWithMessages(name, []Message{{Role: "user", Content: message}}, nil, nil); err == nil {
		t.Fatal("missing key accepted")
	}
	body, err := captureChatBodyForTest(t, "VolcEngine", name, []Message{{Role: "user", Content: message}}, &ChatConfig{Thinking: tailBoolPtr(true), Effort: stringPtr("invalid")}, false)
	if err != nil || body["reasoning_effort"] != "invalid" {
		t.Fatal("frozen effort pass-through", body, err)
	}
}

func TestVolcEngineLargeStreamDelta(t *testing.T) {
	answer := strings.Repeat("a", 100000)
	server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":%q}}]}\n\ndata: [DONE]\n\n", answer)
	}))
	defer server.Close()
	driver := NewVolcEngine(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
	key, name, message := "key", "doubao", "q"
	var got string
	err := driver.ChatStreamlyWithSender(name, []Message{{Role: "user", Content: message}}, &APIConfig{ApiKey: &key}, nil, func(content, reason *string) error {
		if content != nil && *content != "[DONE]" {
			got += *content
		}
		return nil
	})
	if err != nil || got != answer {
		t.Fatalf("large delta: len=%d error=%v", len(got), err)
	}
	if _, err := driver.ChatWithMessages(name, nil, &APIConfig{ApiKey: &key}, nil); err == nil {
		t.Fatal("empty messages accepted")
	}
}

func TestVolcEngineEffortMapping(t *testing.T) {
	for _, stream := range []bool{false, true} {
		for _, test := range []struct{ effort, mode, mapped string }{
			{"", "enabled", "medium"}, {"auto", "enabled", "medium"}, {"default", "enabled", "medium"},
			{"none", "disabled", "minimal"}, {"minimal", "disabled", "minimal"},
			{"low", "enabled", "low"}, {"medium", "enabled", "medium"}, {"high", "enabled", "high"}, {"xhigh", "enabled", "xhigh"},
		} {
			thinking := true
			config := &ChatConfig{Thinking: &thinking}
			if test.effort != "" {
				config.Effort = &test.effort
			}
			body, err := captureChatBodyForTest(t, "VolcEngine", "doubao", []Message{{Role: "user", Content: "q"}}, config, stream)
			if stream && test.effort == "xhigh" {
				if err == nil {
					t.Fatal("invalid streaming effort accepted")
				}
				continue
			}
			if err != nil || body["thinking"].(map[string]any)["type"] != test.mode || body["reasoning_effort"] != test.mapped {
				t.Fatalf("effort %q: %v, %v", test.effort, body, err)
			}
		}
		thinking := false
		body, err := captureChatBodyForTest(t, "VolcEngine", "doubao", []Message{{Role: "user", Content: "q"}}, &ChatConfig{Thinking: &thinking}, stream)
		if err != nil || body["thinking"].(map[string]any)["type"] != "disabled" || body["reasoning_effort"] != nil {
			t.Fatalf("disabled: %v %v", body, err)
		}
	}
}

func tailBoolPtr(value bool) *bool { return &value }
