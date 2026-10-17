package models

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

func TestNVIDIAChatHTTP(t *testing.T) {
	testHostedChatHTTP(t, "NVIDIA", func(t *testing.T, body map[string]any) {
		if !reflect.DeepEqual(body["thinking"], map[string]any{"type": "disabled"}) {
			t.Error("explicit thinking=false lost")
		}
		if body["stream"] == true {
			if body["do_sample"] != false {
				t.Error("stream do_sample=false lost")
			}
		} else if body["do_sample"] != nil {
			t.Error("stream-only option emitted")
		}
	})
}
func TestNVIDIAFailuresHTTP(t *testing.T) { testHostedChatFailures(t, "NVIDIA", false) }
func TestNVIDIACancelHTTP(t *testing.T)   { testHostedCancellation(t, "NVIDIA", false) }

func TestNVIDIAThinkingAndUnsupportedCapabilities(t *testing.T) {
	history := []Message{{Role: "user", Content: "q"}}
	for _, mode := range []string{"omitted", "enabled", "disabled"} {
		t.Run(mode, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/local/v1/chat/completions" {
					t.Error("wrong custom endpoint")
				}
				fmt.Fprint(w, `{"choices":[{"message":{"content":"<think>reason</think>answer"}}]}`)
			}))
			defer server.Close()
			driver := NewNVIDIAModel(map[string]string{"default": server.URL + "/local/v1/"}, URLSuffix{Chat: "chat/completions"})
			config := &ChatConfig{ModelClass: stringPtr("qwen3")}
			if mode != "omitted" {
				on := mode == "enabled"
				config.Thinking = &on
			}
			for _, stream := range []bool{false, true} {
				body, err := nvidiaChatBody("qwen3", history, config, stream)
				if err != nil {
					t.Fatal(err)
				}
				if mode == "omitted" {
					if body["thinking"] != nil {
						t.Fatal("thinking default changed")
					}
				} else if !reflect.DeepEqual(body["thinking"], map[string]string{"type": mode}) {
					t.Fatal(body)
				}
				if body["temperature"] != nil {
					t.Fatal("unrequested temperature default")
				}
			}
			key := "fixture"
			response, err := driver.ChatWithMessages("qwen3", &APIConfig{APIKey: &key}, history, config)
			if err != nil || *response.Answer != "answer" || *response.ReasoningContent != "reason" {
				t.Fatal(response, err)
			}
		})
	}
	driver := NewNVIDIAModel(nil, URLSuffix{})
	name := "m"
	for _, call := range []func() error{
		func() error { _, e := driver.Encode(&name, []string{"q"}, nil, nil); return e },
		func() error { _, e := driver.Rerank(&name, "q", []string{"d"}, nil); return e },
		func() error { _, e := driver.ListModels(nil); return e },
		func() error { _, e := driver.Balance(nil); return e },
		func() error { return driver.CheckConnection(nil) },
	} {
		if err := call(); err == nil {
			t.Fatal("NVIDIA unsupported method succeeded")
		}
	}
	// Installing NVIDIA must keep the previous OpenAI driver live.
	openai, err := NewModelFactory().CreateModelDriver("OpenAI", nil, URLSuffix{})
	if _, ok := openai.(*OpenAIModel); err != nil || !ok {
		t.Fatal("OpenAI factory was regressed", openai, err)
	}
}
