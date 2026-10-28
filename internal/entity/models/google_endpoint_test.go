package models

import (
	"context"
	"fmt"
	"net/http"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
)

func TestGoogleCustomEndpointSDKAndIsolation(t *testing.T) {
	var fallbackCalls atomic.Int32
	fallback := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fallbackCalls.Add(1)
		w.WriteHeader(http.StatusInternalServerError)
	}))
	t.Cleanup(func() {
		if fallbackCalls.Load() != 0 {
			t.Error("custom instances reached the fallback endpoint")
		}
	})
	for _, instance := range []string{"one", "two"} {
		t.Run(instance, func(t *testing.T) {
			t.Parallel()
			var calls atomic.Int32
			server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.Header.Get("x-goog-api-key") != instance || r.URL.Query().Has("key") {
					t.Error("instance authentication changed")
				}
				switch r.URL.Path {
				case "/proxy/" + instance + "/v1beta/models":
					fmt.Fprint(w, `{"models":[{"name":"models/gemini-test"}]}`)
				case "/proxy/" + instance + "/v1beta/models/gemini-test:generateContent":
					fmt.Fprint(w, `{"candidates":[{"content":{"parts":[{"text":"`+instance+`"}]}}]}`)
				case "/proxy/" + instance + "/v1beta/models/gemini-test:streamGenerateContent":
					if r.URL.Query().Get("alt") != "sse" {
						t.Error("SDK stream query changed")
					}
					w.Header().Set("Content-Type", "text/event-stream")
					fmt.Fprintf(w, "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"%s\"}]},\"finishReason\":\"STOP\"}]}\n\n", instance)
				case "/proxy/" + instance + "/v1beta/models/gemini-test:batchEmbedContents":
					fmt.Fprint(w, `{"embeddings":[{"values":[0,1]},{"values":[2,0]}]}`)
				default:
					t.Error("custom SDK path changed", r.URL.Path)
					w.WriteHeader(http.StatusNotFound)
				}
			}))
			urls := map[string]string{"default": fallback.URL, instance: "  " + server.URL + "/proxy/" + instance + "  "}
			driver, err := NewModelFactory().CreateModelDriver("Google", urls, URLSuffix{})
			if err != nil {
				t.Fatal(err)
			}
			urls[instance] = fallback.URL
			api := &APIConfig{APIKey: stringPtr(" " + instance + " "), Region: &instance}
			name, message := "gemini-test", "hello"
			reply, err := driver.ChatWithMessages(name, api, []Message{{Role: "user", Content: message}}, nil)
			if err != nil || reply == nil || reply.Answer == nil || *reply.Answer != instance {
				t.Fatal("custom chat", reply, err)
			}
			var chunks []string
			err = driver.ChatStreamlyWithSender(&name, &message, api, nil, func(answer, reasoning *string) error {
				if answer != nil {
					chunks = append(chunks, *answer)
				}
				return nil
			})
			if err != nil || !reflect.DeepEqual(chunks, []string{instance, "[DONE]"}) {
				t.Fatal("custom stream", chunks, err)
			}
			vectors, err := driver.Embed(&name, []string{"a", "b"}, api, &EmbeddingConfig{Dimension: 2})
			if err != nil || !reflect.DeepEqual(vectors, []EmbeddingData{{Index: 0, Embedding: []float64{0, 1}}, {Index: 1, Embedding: []float64{2, 0}}}) {
				t.Fatal("custom embedding", vectors, err)
			}
			names, err := driver.ListModels(api)
			if err != nil || !reflect.DeepEqual(names, []string{"models/gemini-test"}) {
				t.Fatal("custom discovery", names, err)
			}
			if err := driver.CheckConnection(api); err != nil {
				t.Fatal("custom connection", err)
			}
			if calls.Load() != 5 || api.Context != nil || *api.APIKey != " "+instance+" " || *api.Region != instance {
				t.Fatal("SDK call count or request configuration changed")
			}
		})
	}
}

func TestGoogleRejectsInvalidEndpointBeforeSDK(t *testing.T) {
	for i, endpoint := range []string{
		"/relative", "http://", "http://:1234", "ftp://example.invalid",
		"https://user:private@example.invalid", "https://example.invalid?key=private", "https://example.invalid#private",
	} {
		t.Run(fmt.Sprint(i), func(t *testing.T) {
			driver := NewGoogleModel(map[string]string{"default": "https://generativelanguage.googleapis.com", "custom": endpoint}, URLSuffix{})
			client, err := driver.client(context.Background(), &APIConfig{APIKey: stringPtr("private-key"), Region: stringPtr("custom")})
			if err == nil || client != nil {
				t.Fatal("invalid custom endpoint accepted by SDK client")
			}
			if strings.Contains(err.Error(), "private") || strings.Contains(err.Error(), endpoint) {
				t.Fatal("invalid endpoint error exposed configuration")
			}
		})
	}
}
