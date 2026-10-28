package models

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"
)

func TestClaudeSamplingOutgoingHTTP(t *testing.T) {
	for _, model := range []struct {
		name             string
		noSampling, pair bool
	}{
		{"claude-opus-4-7", true, false}, {"anthropic/claude-opus-4-8", true, false},
		{"claude-opus-4-6", false, false}, {"claude-sonnet-4-6-20260101", false, false},
		{"claude-sonnet-4-5-20250929", false, false}, {"claude-opus-4-1", false, false},
		{"claude-opus-4-20250514", false, true}, {"claude-3-7-sonnet-20250219", false, true},
		{"claude-3-haiku", false, true}, {"unrelated", false, true},
	} {
		for _, bedrock := range []bool{false, true} {
			name := model.name
			if bedrock && strings.HasPrefix(name, "claude-") {
				name = "eu.anthropic." + name + "-v1:0"
			}
			t.Run(fmt.Sprintf("%s/bedrock=%t", name, bedrock), func(t *testing.T) {
				server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					var body map[string]any
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
						return
					}
					fields := body
					p := "top_p"
					if bedrock {
						fields, _ = body["inferenceConfig"].(map[string]any)
						p = "topP"
					}
					if model.noSampling {
						if fields["temperature"] != nil || fields[p] != nil {
							t.Error("rejected sampling sent", fields)
						}
					} else {
						if fields["temperature"] != 0.2 {
							t.Error("temperature lost", fields)
						}
						if model.pair {
							if fields[p] != 0.8 {
								t.Error("old model sampling pair lost", fields)
							}
						} else if fields[p] != nil {
							t.Error("mutually exclusive top_p sent", fields)
						}
					}
					if bedrock {
						fmt.Fprint(w, `{"output":{"message":{"role":"assistant","content":[{"text":"answer"}]}},"stopReason":"end_turn","usage":{"inputTokens":1,"outputTokens":1,"totalTokens":2}}`)
					} else {
						fmt.Fprint(w, `{"content":[{"type":"text","text":"answer"}]}`)
					}
				}))
				config := &ChatConfig{Temperature: anthropicTestPtr(0.2), TopP: anthropicTestPtr(0.8)}
				var response *ChatResponse
				var err error
				if bedrock {
					response, err = NewBedrockModel(map[string]string{"default": server.URL}, URLSuffix{}).ChatWithMessages(name, bedrockTestAPI(), []Message{{Role: "user", Content: "q"}}, config)
				} else {
					response, err = NewAnthropicModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "messages"}).ChatWithMessages(name, &APIConfig{APIKey: stringPtr("fixture")}, []Message{{Role: "user", Content: "q"}}, config)
				}
				if err != nil || response == nil || *response.Answer != "answer" {
					t.Fatal(response, err)
				}
				if *config.Temperature != 0.2 || *config.TopP != 0.8 {
					t.Fatal("caller mutated")
				}
			})
		}
	}
}
