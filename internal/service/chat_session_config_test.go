package service

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sync/atomic"
	"testing"

	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

// Exercise the current session consumer through the bound model and HTTP driver.
func TestChatSessionJSONGenerationConfig(t *testing.T) {
	for _, tc := range []struct {
		name      string
		stored    entity.JSONMap
		request   map[string]interface{}
		maxTokens int
		stop      []string
	}{
		{"stored-json", entity.JSONMap{"max_tokens": float64(256), "stop": []interface{}{"stored"}}, nil, 256, []string{"stored"}},
		{"request-json-override", entity.JSONMap{"max_tokens": float64(256), "stop": []interface{}{"stored"}}, map[string]interface{}{"max_tokens": float64(128), "stop": []interface{}{"END", "DONE"}}, 128, []string{"END", "DONE"}},
		{"typed-config", entity.JSONMap{"max_tokens": 64, "stop": []string{"typed"}}, nil, 64, []string{"typed"}},
		{"explicit-zero-and-empty", entity.JSONMap{"max_tokens": 64, "stop": []string{"typed"}}, map[string]interface{}{"max_tokens": float64(0), "stop": []interface{}{}}, 0, []string{}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			for _, stream := range []bool{false, true} {
				fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					var body struct {
						MaxTokens *int     `json:"max_tokens"`
						Stop      []string `json:"stop"`
					}
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
					}
					if body.MaxTokens == nil || *body.MaxTokens != tc.maxTokens || !reflect.DeepEqual(body.Stop, tc.stop) {
						t.Errorf("stream=%v generation config lost: %#v", stream, body)
					}
					if stream {
						fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
					} else {
						fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
					}
				}))
				t.Cleanup(fixture.Close)
				name, key := "model", "fixture-key"
				model := models.NewChatModel(models.NewMoonshotModel(map[string]string{"default": fixture.URL}, models.URLSuffix{Chat: "chat"}), &name, &models.APIConfig{APIKey: &key})
				values := (&ChatSessionService{}).buildGenConf(&entity.Chat{LLMSetting: tc.stored}, tc.request)
				history := []map[string]string{{"role": "user", "content": "question"}}
				if stream {
					if err := model.ChatStreamlyWithSender("rules", history, values, func(*string, *string) error { return nil }); err != nil {
						t.Fatal(err)
					}
				} else if answer, err := model.Chat("rules", history, values); err != nil || answer != "answer" {
					t.Fatalf("answer=%q error=%v", answer, err)
				}
				fixture.Close()
			}
		})
	}
}

func TestChatSessionRejectsMalformedJSONGenerationConfig(t *testing.T) {
	var requests atomic.Int32
	fixture := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { requests.Add(1) }))
	defer fixture.Close()
	name, key := "model", "fixture-key"
	model := models.NewChatModel(models.NewMoonshotModel(map[string]string{"default": fixture.URL}, models.URLSuffix{Chat: "chat"}), &name, &models.APIConfig{APIKey: &key})
	for _, raw := range []string{`{"max_tokens":1.5}`, `{"max_tokens":1e100}`, `{"max_tokens":"128"}`, `{"stop":["valid",42]}`, `{"stop":"END"}`} {
		var config map[string]interface{}
		if err := json.Unmarshal([]byte(raw), &config); err != nil {
			t.Fatal(err)
		}
		for _, stream := range []bool{false, true} {
			values := (&ChatSessionService{}).buildGenConf(&entity.Chat{}, config)
			var err error
			if stream {
				err = model.ChatStreamlyWithSender("", nil, values, func(*string, *string) error { return nil })
			} else {
				_, err = model.Chat("", nil, values)
			}
			if err == nil {
				t.Fatalf("malformed config accepted: %s stream=%v", raw, stream)
			}
		}
	}
	if requests.Load() != 0 {
		t.Fatal("malformed config reached provider")
	}
}
