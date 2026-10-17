package cli

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestChatEOFAndTimeoutFeedbackHTTP(t *testing.T) {
	for _, stream := range []bool{false, true} {
		for _, mode := range []string{"EOF", "timeout", "body EOF", "body timeout"} {
			if !stream && mode == "body timeout" {
				continue
			}
			t.Run(fmt.Sprintf("%s stream=%t", mode, stream), func(t *testing.T) {
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					_, _ = io.Copy(io.Discard, r.Body)
					_ = r.Body.Close()
					if r.URL.Path != "/api/v1/chat/completions" {
						t.Error("wrong chat route")
					}
					switch mode {
					case "EOF":
						connection, _, err := w.(http.Hijacker).Hijack()
						if err != nil {
							t.Error(err)
							return
						}
						connection.Close()
					case "timeout":
						select {
						case <-r.Context().Done():
						case <-time.After(2 * time.Second):
							t.Error("HTTP request did not cancel")
						}
					case "body timeout":
						w.Header().Set("Content-Type", "text/event-stream")
						fmt.Fprint(w, "data: [MESSAGE]partial\n\n")
						w.(http.Flusher).Flush()
						select {
						case <-r.Context().Done():
						case <-time.After(2 * time.Second):
							t.Error("HTTP request did not cancel")
						}
					case "body EOF":
						if stream {
							fmt.Fprint(w, "data: [MESSAGE]partial\n\n")
						} else {
							w.Header().Set("Content-Length", "100")
							fmt.Fprint(w, `{"code":`)
						}
					}
				}))
				defer server.Close()
				u, _ := url.Parse(server.URL)
				port, _ := strconv.Atoi(u.Port())
				client := NewMultiRAGClient("user")
				client.HTTPClient.Host = u.Hostname()
				client.HTTPClient.Port = port
				client.HTTPClient.client.Timeout = 75 * time.Millisecond
				input := `CHAT "m@i@NVIDIA" "q";`
				if stream {
					input = `STREAM CHAT "m@i@NVIDIA" "q";`
				}
				command, err := NewParser(input).Parse(false)
				if err != nil {
					t.Fatal(err)
				}
				response, err := client.ExecuteUserCommand(command)
				if err == nil || response != nil {
					t.Fatal("transport failure succeeded", response, err)
				}
				word := "EOF"
				if strings.Contains(mode, "timeout") {
					word = "timeout"
				}
				if !strings.Contains(err.Error(), word) || strings.Contains(err.Error(), "list instance models") {
					t.Fatal(err)
				}
				if word == "EOF" && !errors.Is(err, io.EOF) && !errors.Is(err, io.ErrUnexpectedEOF) {
					t.Fatal("EOF cause lost", err)
				}
				if word == "timeout" && !errors.Is(err, context.DeadlineExceeded) {
					t.Fatal("timeout cause lost", err)
				}
				t.Log("chat EOF/timeout feedback verified through parser, request, and actual HTTP transport")
			})
		}
	}
}

func TestChatRequestErrorPreservesOtherCause(t *testing.T) {
	want := errors.New("transport rejected")
	err := chatRequestError(want)
	if !errors.Is(err, want) || !strings.Contains(err.Error(), "failed to chat model") {
		t.Fatal(err)
	}
}
