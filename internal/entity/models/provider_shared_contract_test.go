package models

import (
	"bufio"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
)

type providerContractTransport func(*http.Request) (*http.Response, error)

func (f providerContractTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestProviderSharedOptionalKeyAndThinking(t *testing.T) {
	for _, stream := range []bool{false, true} {
		for _, enabled := range []bool{false, true} {
			t.Run(strings.Join([]string{map[bool]string{true: "stream", false: "sync"}[stream], map[bool]string{true: "optional", false: "required"}[enabled]}, "/"), func(t *testing.T) {
				calls := 0
				d := newGatewayChatDriver(map[string]string{"default": "http://controlled.invalid"}, URLSuffix{Chat: "chat"}, "modelscope")
				d.optionalAPIKey, d.thinkingAlias, d.sseEvents = enabled, true, true
				d.httpClient.Transport = providerContractTransport(func(r *http.Request) (*http.Response, error) {
					calls++
					if r.Header.Get("Authorization") != "" {
						t.Error("keyless request has auth")
					}
					raw := `{"choices":[{"message":{"content":"answer","thinking":"thought"}}]}`
					if stream {
						raw = "data: {\"choices\":[{\"delta\":{\"content\":\"answer\",\"thinking\":\"thought\"}}]}\n\ndata: [DONE]\n\n"
					}
					return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(raw)), Header: http.Header{}}, nil
				})
				var err error
				reason := ""
				if stream {
					err = d.ChatStreamlyWithMessages("model", []Message{{Role: "user", Content: "q"}}, nil, nil, func(a, r *string) error {
						if r != nil {
							reason += *r
						}
						return nil
					})
				} else {
					var result *ChatResponse
					result, err = d.ChatWithMessages("model", nil, []Message{{Role: "user", Content: "q"}}, nil)
					if result != nil && result.ReasoningContent != nil {
						reason = *result.ReasoningContent
					}
				}
				if enabled && (err != nil || calls != 1 || reason != "thought") || !enabled && (err == nil || calls != 0) {
					t.Fatal(err, calls, reason)
				}
			})
		}
	}
}
func TestProviderSharedSSEPartialFrame(t *testing.T) {
	for _, events := range []bool{false, true} {
		_, ok, err := nextOpenAIStreamData(bufio.NewScanner(strings.NewReader("data: [DONE]\n")), events)
		if events && (!errors.Is(err, io.ErrUnexpectedEOF) || ok) || !events && (err != nil || !ok) {
			t.Fatal(events, ok, err)
		}
	}
}
