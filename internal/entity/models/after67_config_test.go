package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
)

func after67ChatDriver(t *testing.T, provider string, urls map[string]string) ModelDriver {
	t.Helper()
	driver, err := NewModelFactory().CreateModelDriver(provider, urls, URLSuffix{Chat: "chat/completions", Models: "models"})
	if err != nil {
		t.Fatal(err)
	}
	return driver
}

func TestAfter67ChatConfigIsolationHTTP(t *testing.T) {
	for _, provider := range []string{"DeepSeek", "Gitee", "SiliconFlow"} {
		t.Run(provider, func(t *testing.T) {
			var calls atomic.Int32
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if r.URL.Path != "/instance/chat/completions" || r.Header.Get("Authorization") != "Bearer key" {
					t.Error("request lost instance endpoint or key")
				}
				if body["temperature"] != float64(0) || body["top_p"] != float64(0) || body["max_tokens"] != float64(0) || !reflect.DeepEqual(body["stop"], []any{}) {
					t.Error("explicit zero or empty selection changed", body)
				}
				if body["stream"] == true {
					fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
				} else {
					fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
				}
			}))
			driver := after67ChatDriver(t, provider, map[string]string{"default": srv.URL + "/instance"})
			zero, tokens, no := float64(0), 0, false
			empty := []string{}
			config := &ChatConfig{Temperature: &zero, TopP: &zero, MaxTokens: &tokens, Stop: &empty, Thinking: &no}
			before := *config
			var group sync.WaitGroup
			for range 8 {
				group.Go(func() {
					api := &APIConfig{APIKey: stringPtr("key")}
					messages := []Message{{Role: "user", Content: "q"}}
					if got, err := driver.ChatWithMessages("served", api, messages, config); err != nil || got == nil || *got.Answer != "answer" {
						t.Error(got, err)
					}
					done := 0
					if err := driver.ChatStreamlyWithMessages("served", messages, api, config, func(content, _ *string) error {
						if content != nil && *content == "[DONE]" {
							done++
						}
						return nil
					}); err != nil || done != 1 {
						t.Error(err, done)
					}
				})
			}
			group.Wait()
			if !reflect.DeepEqual(*config, before) || calls.Load() != 16 {
				t.Fatal("shared caller settings changed or missing requests", calls.Load())
			}
		})
	}
}

func TestAfter67ChatPreflightHasNoHTTP(t *testing.T) {
	for _, provider := range []string{"DeepSeek", "Gitee", "SiliconFlow"} {
		t.Run(provider, func(t *testing.T) {
			var calls atomic.Int32
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
			}))
			driver := after67ChatDriver(t, provider, map[string]string{"default": srv.URL})
			for _, api := range []*APIConfig{nil, {}, {APIKey: stringPtr("")}, {APIKey: stringPtr(" \t ")}, {APIKey: stringPtr("key"), Region: stringPtr("unknown")}} {
				if got, err := driver.ChatWithMessages("served", api, []Message{{Role: "user", Content: "q"}}, nil); err == nil || got != nil {
					t.Error("invalid API configuration accepted")
				}
				if err := driver.ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "q"}}, api, nil, func(*string, *string) error { return nil }); err == nil {
					t.Error("invalid streaming API configuration accepted")
				}
			}
			if calls.Load() != 0 {
				t.Fatal("invalid settings reached provider", calls.Load())
			}
		})
	}
}

func TestAfter67ChatResponseStrictness(t *testing.T) {
	valid := `{"choices":[{"message":{"content":"answer"}}]}`
	for _, provider := range []string{"DeepSeek", "Gitee", "SiliconFlow", "Aliyun"} {
		for _, raw := range []string{
			`{"error":{"message":"private-secret"},"choices":[{"message":{"content":"answer"}}]}`,
			`{"choices":[{"message":{"content":"   "}}]}`,
			`{"choices":[{"finish_reason":"private-secret","message":{"content":"answer"}}]}`,
			valid + ` {}`,
			`{"choices":[{"message":{"content":42}}]}`,
		} {
			t.Run(provider+"/"+raw, func(t *testing.T) {
				driver := after67ChatDriver(t, provider, map[string]string{"default": "http://controlled.invalid"})
				after67Transport(driver).Transport = providerContractTransport(func(*http.Request) (*http.Response, error) {
					return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(raw))}, nil
				})
				got, err := driver.ChatWithMessages("served", &APIConfig{APIKey: stringPtr("key")}, []Message{{Role: "user", Content: "q"}}, nil)
				if err == nil || got != nil || strings.Contains(err.Error(), "private-secret") {
					t.Fatal("malformed response accepted or raw error exposed", got, err)
				}
			})
		}
		for _, invalid := range []string{`{"choices":[{"delta":{"content":42}}]}`, `{"choices":[null]}`, `{"choices":null}`, `{"choices":[{"delta":null}]}`, `{"choices":[{"finish_reason":"private-secret","delta":{}}]}`, `{"choices":[{"delta":{"reasoning_content":42}}]}`} {
			t.Run(provider+"/stream/"+invalid, func(t *testing.T) {
				driver := after67ChatDriver(t, provider, map[string]string{"default": "http://controlled.invalid"})
				after67Transport(driver).Transport = providerContractTransport(func(*http.Request) (*http.Response, error) {
					body := "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: " + invalid + "\n\ndata: [DONE]\n\n"
					return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body))}, nil
				})
				done := false
				err := driver.ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: stringPtr("key")}, nil, func(content, _ *string) error {
					if content != nil && *content == "[DONE]" {
						done = true
					}
					return nil
				})
				if err == nil || done || strings.Contains(err.Error(), "private-secret") {
					t.Fatal("malformed stream completed or raw error exposed", err, done)
				}
			})
		}
	}
}

func after67Transport(driver ModelDriver) *http.Client {
	switch d := driver.(type) {
	case *DeepSeekModel:
		return d.httpClient
	case *GiteeModel:
		return d.httpClient
	case *AliyunModel:
		return d.httpClient
	default:
		return driver.(*SiliconFlowModel).httpClient
	}
}

func TestAfter67DeepSeekEndpointAndThinkingCopies(t *testing.T) {
	urls := map[string]string{"default": "http://first.invalid"}
	driver := NewDeepSeekModel(urls, URLSuffix{Chat: "custom/chat"})
	urls["default"] = "http://second.invalid"
	for _, stream := range []bool{false, true} {
		yes := true
		config := &ChatConfig{Thinking: &yes, Effort: stringPtr("low")}
		driver.httpClient.Transport = providerContractTransport(func(r *http.Request) (*http.Response, error) {
			if r.URL.String() != "http://first.invalid/custom/chat" {
				t.Error("caller map or ignored suffix changed endpoint", r.URL)
			}
			body := `{"choices":[{"message":{"content":"answer"}}]}`
			if stream {
				body = "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n"
			}
			return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body))}, nil
		})
		api := &APIConfig{APIKey: stringPtr("key")}
		if stream {
			if err := driver.ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "q"}}, api, config, func(*string, *string) error { return nil }); err != nil {
				t.Fatal(err)
			}
		} else if _, err := driver.ChatWithMessages("served", api, []Message{{Role: "user", Content: "q"}}, config); err != nil {
			t.Fatal(err)
		}
		if config.Thinking == nil || !*config.Thinking || config.Verbosity != nil {
			t.Fatal("request changed caller thinking settings")
		}
	}
}

func TestAfter67ChatEndpointIsolationAndDefaults(t *testing.T) {
	for _, provider := range []string{"DeepSeek", "Gitee", "SiliconFlow", "Aliyun"} {
		t.Run(provider, func(t *testing.T) {
			urls := map[string]string{"default": "http://default.invalid", "office": "http://office.invalid"}
			driver := after67ChatDriver(t, provider, urls)
			urls["default"] = "http://mutated.invalid"
			calls := 0
			client := after67Transport(driver)
			client.Transport = providerContractTransport(func(r *http.Request) (*http.Response, error) {
				calls++
				if r.Header.Get("Authorization") != "Bearer key" {
					t.Error("untrimmed key")
				}
				host := "default.invalid"
				if calls == 2 {
					host = "office.invalid"
				}
				if r.URL.Host != host || r.URL.Path != "/chat/completions" {
					t.Error("wrong region or caller map leaked", r.URL)
				}
				return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(`{"choices":[{"message":{"content":"answer"}}]}`))}, nil
			})
			key := " key "
			for _, region := range []*string{nil, stringPtr("office"), stringPtr("")} {
				if _, err := driver.ChatWithMessages("served", &APIConfig{APIKey: &key, Region: region}, []Message{{Role: "user", Content: "q"}}, nil); err != nil {
					t.Fatal(err)
				}
			}
			if key != " key " || calls != 3 {
				t.Fatal("caller credentials modified or request lost")
			}
			for _, api := range []*APIConfig{nil, {}, {APIKey: stringPtr(" 	 ")}, {APIKey: stringPtr("key"), Region: stringPtr("missing")}} {
				if _, err := driver.ChatWithMessages("served", api, []Message{{Role: "user", Content: "q"}}, nil); err == nil {
					t.Error("bad key/region accepted")
				}
			}
			if calls != 3 {
				t.Fatal("invalid API settings made HTTP call")
			}
		})
	}
}

func TestAfter67StreamTerminalFailureAndCancellation(t *testing.T) {
	for _, provider := range []string{"DeepSeek", "Gitee", "SiliconFlow", "Aliyun"} {
		for _, raw := range []string{
			"data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\n",
			"data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n",
			"data: {\"choices\":[{\"delta\":{\"content\":\"answer\"},\"finish_reason\":\"stop\"}]}\n\ndata: {\"choices\":[null]}\n\ndata: [DONE]\n\n",
			"data: {\"choices\":[{\"delta\":{\"content\":\"   \"}}]}\n\ndata: [DONE]\n\n",
		} {
			t.Run(provider+"/"+raw, func(t *testing.T) {
				d := after67ChatDriver(t, provider, map[string]string{"default": "http://controlled.invalid"})
				after67Transport(d).Transport = providerContractTransport(func(*http.Request) (*http.Response, error) {
					return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(raw))}, nil
				})
				done := false
				err := d.ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: stringPtr("key")}, nil, func(c, _ *string) error { done = done || c != nil && *c == "[DONE]"; return nil })
				if err == nil || done {
					t.Fatal("invalid terminal became success", err, done)
				}
			})
		}
		d := after67ChatDriver(t, provider, map[string]string{"default": "http://controlled.invalid"})
		raw := "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n"
		after67Transport(d).Transport = providerContractTransport(func(*http.Request) (*http.Response, error) {
			return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(raw))}, nil
		})
		sentinel := errors.New("sender stopped")
		ctx, cancel := context.WithCancel(t.Context())
		for _, cancelInSender := range []bool{false, true} {
			err := d.ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: stringPtr("key"), Context: ctx}, nil, func(*string, *string) error {
				if cancelInSender {
					cancel()
					return nil
				}
				return sentinel
			})
			want := sentinel
			if cancelInSender {
				want = context.Canceled
			}
			if !errors.Is(err, want) {
				t.Fatal(provider, err, want)
			}
		}
		cancel()
	}
}

func TestAfter67GiteeProbeRejectsMissingKey(t *testing.T) {
	d := NewGiteeModel(map[string]string{"default": "http://controlled.invalid"}, URLSuffix{Status: "status"})
	calls := 0
	d.httpClient.Transport = providerContractTransport(func(*http.Request) (*http.Response, error) {
		calls++
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(""))}, nil
	})
	for _, api := range []*APIConfig{nil, {}, {APIKey: stringPtr(" ")}, {APIKey: stringPtr("key"), Region: stringPtr("missing")}} {
		if err := d.CheckConnection(api); err == nil {
			t.Fatal("invalid probe configuration accepted")
		}
	}
	if calls != 0 {
		t.Fatal("invalid probe reached HTTP")
	}
}

func TestAfter67GiteeTailFlushCancellation(t *testing.T) {
	d := NewGiteeModel(map[string]string{"default": "http://controlled.invalid"}, URLSuffix{Chat: "chat"})
	raw := "data: {\"choices\":[{\"delta\":{\"content\":\"<\"}}]}\n\ndata: [DONE]\n\n"
	d.httpClient.Transport = providerContractTransport(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(raw))}, nil
	})
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	done := false
	err := d.ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: stringPtr("key"), Context: ctx}, nil, func(content, _ *string) error {
		if content != nil && *content == "[DONE]" {
			done = true
		}
		cancel()
		return nil
	})
	if !errors.Is(err, context.Canceled) || done {
		t.Fatal("cancelled tail flush completed", err, done)
	}
}

func TestAfter67EmptyFramesUsageAndTerminalPolicy(t *testing.T) {
	answer := "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\n"
	stop := "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n"
	for _, provider := range []string{"DeepSeek", "Gitee", "SiliconFlow", "Aliyun"} {
		for _, tc := range []struct {
			suffix string
			want   bool
		}{
			{"data: [DONE]\n\n", true},
			{"data: {\"choices\":[],\"usage\":{}}\n\ndata: [DONE]\n\n", true},
			{"data: {\"choices\":null,\"usage\":{}}\n\ndata: [DONE]\n\n", false},
			{"data: {\"usage\":{}}\n\ndata: [DONE]\n\n", false},
			{stop, provider != "Aliyun"},
		} {
			t.Run(provider+"/"+tc.suffix, func(t *testing.T) {
				d := after67ChatDriver(t, provider, map[string]string{"default": "http://controlled.invalid"})
				raw := "data:\n\ndata: \n\n" + answer + tc.suffix
				after67Transport(d).Transport = providerContractTransport(func(*http.Request) (*http.Response, error) {
					return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(raw))}, nil
				})
				done := false
				err := d.ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "q"}}, &APIConfig{APIKey: stringPtr("key")}, nil, func(c, _ *string) error { done = done || c != nil && *c == "[DONE]"; return nil })
				if (err == nil) != tc.want || done != tc.want {
					t.Fatal(err, done, tc.want)
				}
			})
		}
	}
}

type after67CancelledBody struct {
	ctx    context.Context
	cancel context.CancelFunc
}

func (b after67CancelledBody) Read([]byte) (int, error) { b.cancel(); return 0, b.ctx.Err() }
func (b after67CancelledBody) Close() error             { return nil }

func TestAfter67BodyReadCancellation(t *testing.T) {
	for _, provider := range []string{"DeepSeek", "Gitee", "SiliconFlow", "Aliyun"} {
		t.Run(provider, func(t *testing.T) {
			d := after67ChatDriver(t, provider, map[string]string{"default": "http://controlled.invalid"})
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			after67Transport(d).Transport = providerContractTransport(func(*http.Request) (*http.Response, error) {
				return &http.Response{StatusCode: 200, Body: after67CancelledBody{ctx: ctx, cancel: cancel}}, nil
			})
			result, err := d.ChatWithMessages("served", &APIConfig{APIKey: stringPtr("key"), Context: ctx}, []Message{{Role: "user", Content: "q"}}, nil)
			if !errors.Is(err, context.Canceled) || result != nil {
				t.Fatal(result, err)
			}
		})
	}
	d := NewDeepSeekModel(map[string]string{"default": "http://controlled.invalid"}, URLSuffix{Models: "models"})
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	d.httpClient.Transport = providerContractTransport(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Body: after67CancelledBody{ctx: ctx, cancel: cancel}}, nil
	})
	if names, err := d.ListModels(&APIConfig{APIKey: stringPtr("key"), Context: ctx}); !errors.Is(err, context.Canceled) || names != nil {
		t.Fatal(names, err)
	}
}
