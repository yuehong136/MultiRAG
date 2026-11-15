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

func TestXunFeiAliasesBundleAndThinkingHTTP(t *testing.T) {
	for _, name := range []string{"spark-x", "Spark-Max", "Spark-Max-32K", "Spark-Lite", "Spark-Pro", "Spark-Pro-128K", "Spark-4.0-Ultra"} {
		t.Run(name, func(t *testing.T) {
			calls := 0
			fixture := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				path := "/v2/chat/completions"
				model := name
				if r.URL.Path != path || r.Header.Get("Authorization") != "Bearer password" {
					t.Error("Spark path/credential", r.URL.Path)
				}
				var body map[string]any
				json.NewDecoder(r.Body).Decode(&body)
				if body["model"] != model || !reflect.DeepEqual(body["thinking"], map[string]any{"type": "disabled"}) {
					t.Error("Spark name/thinking", body)
				}
				if body["stream"] == true {
					w.Header().Set("Content-Type", "text/event-stream")
					fmt.Fprint(w, "data: {\"code\":0,\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
				} else {
					fmt.Fprint(w, `{"code":0,"choices":[{"message":{"content":"answer"}}]}`)
				}
			}))
			defer fixture.Close()
			d := NewXunFeiModel(map[string]string{"default": fixture.URL}, URLSuffix{Chat: "v2/chat/completions"})
			key := "password"
			api := &APIConfig{ApiKey: &key}
			config := &ChatConfig{Thinking: anthropicTestPtr(false)}
			response, err := d.ChatWithMessages(name, []Message{{Role: "user", Content: "q"}}, api, config)
			if err != nil || response == nil || *response.Answer != "answer" {
				t.Fatal(response, err)
			}
			if err := d.ChatStreamlyWithSender(name, []Message{{Role: "user", Content: "q"}}, api, config, func(*string, *string) error { return nil }); err != nil {
				t.Fatal(err)
			}
			if calls != 2 || api.Context != nil || *api.ApiKey != "password" || d.baseModel.URLSuffix.Chat != "v2/chat/completions" {
				t.Fatal("instance mutated", calls)
			}
		})
	}
}

func TestXunFeiCheckConnectionAndStaticDiscovery(t *testing.T) {
	calls := 0
	fixture := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Method != "GET" || r.URL.Path != "/models" || r.Header.Get("Authorization") != "Bearer key" || r.ContentLength != 0 {
			t.Error("frozen model discovery request", r.URL)
		}
		fmt.Fprint(w, `{"data":[{"id":"spark-x"}]}`)
	}))
	defer fixture.Close()
	d := NewXunFeiModel(map[string]string{"default": fixture.URL}, URLSuffix{Chat: "v2/chat/completions", Models: "models"})
	api := &APIConfig{ApiKey: stringPtr("key")}
	names, err := d.ListModels(api)
	if err != nil || !reflect.DeepEqual(modelNamesForTest(names), []string{"spark-x"}) || calls != 1 {
		t.Fatal(names, err, calls)
	}
	if err := d.CheckConnection(api); err == nil || calls != 1 {
		t.Fatal("unsupported connection check returned success", err, calls)
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if names, err := d.ListModels(&APIConfig{ApiKey: api.ApiKey, Context: ctx}); !errors.Is(err, context.Canceled) || names != nil {
		t.Fatal(names, err)
	}
}

func TestXunFeiRejectsInvalidCredentialsAndProviderCode(t *testing.T) {
	d := NewXunFeiModel(map[string]string{"default": "http://unused.invalid"}, URLSuffix{Chat: "chat"})
	calls := 0
	d.baseModel.httpClient.Transport = lmStudioTransportFunc(func(r *http.Request) (*http.Response, error) { calls++; return nil, errors.New("unexpected request") })
	for _, key := range []string{"", " "} {
		api := &APIConfig{ApiKey: &key}
		if response, err := d.ChatWithMessages("spark-x", []Message{{Role: "user", Content: "q"}}, api, nil); err == nil || response != nil || strings.Contains(err.Error(), key) && key != "" && key != " " {
			t.Fatal(response, err)
		}
		if err := d.CheckConnection(api); err == nil {
			t.Fatal("invalid key accepted")
		}
	}
	if calls != 0 {
		t.Fatal("bad credentials reached provider")
	}
	for _, code := range []string{`null`, `1`, `"0"`, `0.5`} {
		fixture := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			var body map[string]any
			json.NewDecoder(r.Body).Decode(&body)
			if body["stream"] == true {
				fmt.Fprintf(w, "data: {\"code\":%s,\"choices\":[{\"delta\":{\"content\":\"invalid\"}}]}\n\ndata: [DONE]\n\n", code)
			} else {
				fmt.Fprintf(w, `{"code":%s,"choices":[{"message":{"content":"invalid"}}]}`, code)
			}
		}))
		d := NewXunFeiModel(map[string]string{"default": fixture.URL}, URLSuffix{Chat: "chat"})
		api := &APIConfig{ApiKey: stringPtr("key")}
		if result, err := d.ChatWithMessages("spark-x", []Message{{Role: "user", Content: "q"}}, api, nil); err == nil || result != nil {
			t.Fatal(result, err)
		}
		done := 0
		if err := d.ChatStreamlyWithSender("spark-x", []Message{{Role: "user", Content: "q"}}, api, nil, func(c, r *string) error {
			if c != nil && *c == "[DONE]" {
				done++
			}
			return nil
		}); err == nil || done != 0 {
			t.Fatal(err, done)
		}
		fixture.Close()
	}
	if _, err := d.Embed(nil, nil, nil, nil); err == nil {
		t.Fatal("unsupported embedding")
	}
	if _, err := d.Balance(nil); err == nil {
		t.Fatal("unsupported balance")
	}

}
