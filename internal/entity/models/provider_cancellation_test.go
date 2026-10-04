package models

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestInstanceProvidersCancelDuringStream(t *testing.T) {
	for _, provider := range []string{"VolcEngine", "vllm"} {
		t.Run(provider, func(t *testing.T) {
			stopped := make(chan struct{})
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n")
				w.(http.Flusher).Flush()
				<-r.Context().Done()
				close(stopped)
			}))
			defer server.Close()
			driver, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Chat: "chat/completions"})
			if err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			key, name, message := "fixture", "model", "q"
			done := false
			err = driver.ChatStreamlyWithSender(&name, &message, &APIConfig{APIKey: &key, Context: ctx}, nil, func(content, reason *string) error {
				if content != nil && *content == "[DONE]" {
					done = true
				}
				if content != nil && *content == "partial" {
					cancel()
				}
				return nil
			})
			if !errors.Is(err, context.Canceled) || done {
				t.Fatalf("cancel: %v, done=%t", err, done)
			}
			<-stopped
		})
	}
}
