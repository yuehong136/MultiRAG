package models

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"reflect"
	"strings"
	"testing"
)

func remainingPoolDrivers(url string) map[string]struct {
	driver ModelDriver
	client *http.Client
} {
	urls := map[string]string{"default": url}
	suffix := URLSuffix{Chat: "chat", Models: "models"}
	a, g, s, z, l := NewAliyunModel(urls, suffix), NewGiteeModel(urls, suffix), NewSiliconFlowModel(urls, suffix), NewZhipuAIModel(urls, suffix), NewLMStudioModel(urls, suffix)
	return map[string]struct {
		driver ModelDriver
		client *http.Client
	}{"Aliyun": {a, a.httpClient}, "Gitee": {g, g.httpClient}, "SiliconFlow": {s, s.httpClient}, "ZHIPU-AI": {z, z.httpClient}, "LM-Studio": {l, l.httpClient}}
}

func TestRemainingDriverPoolReuseAndCredentialIsolation(t *testing.T) {
	for _, provider := range []string{"Aliyun", "Gitee", "SiliconFlow", "ZHIPU-AI", "LM-Studio"} {
		t.Run(provider, func(t *testing.T) {
			var addresses, keys []string
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				io.Copy(io.Discard, r.Body)
				addresses = append(addresses, r.RemoteAddr)
				keys = append(keys, r.Header.Get("Authorization"))
				fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
			}))
			first, second := remainingPoolDrivers(srv.URL)[provider], remainingPoolDrivers(srv.URL)[provider]
			if first.client == second.client || first.client.Transport != openAIChatTransport || second.client.Transport != openAIChatTransport || first.client.Timeout != 0 {
				t.Fatal("pool/client lifetime")
			}
			for i, driver := range []ModelDriver{first.driver, second.driver} {
				key := fmt.Sprint(i)
				result, err := driver.ChatWithMessages("served", &APIConfig{APIKey: &key}, []Message{{Role: "user", Content: "question"}}, nil)
				if err != nil || result == nil || *result.Answer != "answer" {
					t.Fatal(result, err)
				}
			}
			if len(addresses) != 2 || addresses[0] != addresses[1] || !reflect.DeepEqual(keys, []string{"Bearer 0", "Bearer 1"}) {
				t.Fatal(addresses, keys)
			}
			ctx, cancel := context.WithCancel(t.Context())
			cancel()
			_, err := first.driver.ChatWithMessages("served", &APIConfig{APIKey: stringPtr("key"), Context: ctx}, []Message{{Role: "user", Content: "question"}}, nil)
			if !errors.Is(err, context.Canceled) || len(addresses) != 2 {
				t.Fatal("cancellation", err, addresses)
			}
		})
	}
}

func TestRemainingChatStrictFramingAndJSON(t *testing.T) {
	for _, provider := range []string{"LM-Studio", "ZHIPU-AI"} {
		for _, mode := range []string{"multiline", "incomplete", "bad-json", "cancel", "sender"} {
			t.Run(provider+"/"+mode, func(t *testing.T) {
				srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					io.Copy(io.Discard, r.Body)
					w.Header().Set("Content-Type", "text/event-stream")
					if mode == "bad-json" {
						fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]} {}`)
						return
					}
					data := "data: {\"choices\":[\ndata: {\"delta\":{\"content\":\"answer\"}}]}"
					if mode == "incomplete" {
						fmt.Fprint(w, data)
						return
					}
					fmt.Fprint(w, data+"\n\ndata: [DONE]\n\n")
				}))
				driver := remainingPoolDrivers(srv.URL)[provider].driver
				api := &APIConfig{APIKey: stringPtr("key")}
				if mode == "bad-json" {
					if r, e := driver.ChatWithMessages("served", api, []Message{{Role: "user", Content: "q"}}, nil); e == nil || r != nil {
						t.Fatal(r, e)
					}
					return
				}
				ctx, cancel := context.WithCancel(t.Context())
				defer cancel()
				api.Context = ctx
				frames := []string{}
				sentinel := errors.New("sender failed")
				err := driver.ChatStreamlyWithMessages("served", []Message{{Role: "user", Content: "q"}}, api, nil, func(content, _ *string) error {
					if content != nil {
						frames = append(frames, *content)
					}
					if mode == "cancel" {
						cancel()
					}
					if mode == "sender" {
						return sentinel
					}
					return nil
				})
				switch mode {
				case "multiline":
					if err != nil || !reflect.DeepEqual(frames, []string{"answer", "[DONE]"}) {
						t.Fatal(frames, err)
					}
				case "incomplete":
					if !errors.Is(err, io.ErrUnexpectedEOF) || len(frames) > 0 {
						t.Fatal(frames, err)
					}
				case "cancel":
					if !errors.Is(err, context.Canceled) || !reflect.DeepEqual(frames, []string{"answer"}) {
						t.Fatal(frames, err)
					}
				case "sender":
					if !errors.Is(err, sentinel) || !reflect.DeepEqual(frames, []string{"answer"}) {
						t.Fatal(frames, err)
					}
				}
			})
		}
	}
}

func TestProviderPoolMatrix(t *testing.T) {
	// Every driver touched by the transport consolidation must reuse a pool across
	// instances. Distinct clients allow per-driver redirect/timeout policy.
	providers := []string{"302.AI", "Aliyun", "Anthropic", "Astraflow", "Avian", "Azure-OpenAI", "Baichuan", "Baidu", "Bedrock", "Cohere", "CometAPI", "DeepInfra", "DeepSeek", "FishAudio", "FuturMix", "Gitee", "GPUStack", "Groq", "HuaweiCloud", "HuggingFace", "HunYuan", "JieKouAI", "Jina", "LM-Studio", "LocalAI", "LongCat", "MinerU", "mineru_local", "MiniMax", "Mistral", "ModelScope", "Moonshot", "n1n", "Novita", "NVIDIA", "Ollama", "OpenAI", "OpenRouter", "OrcaRouter", "PaddleOCR", "PaddleOCR_Local", "Perplexity", "PPIO", "Qiniu", "Replicate", "SiliconFlow", "StepFun", "TogetherAI", "TokenHub", "TokenPony", "Upstage", "vllm", "VolcEngine", "Voyage AI", "xAI", "Xiaomi", "Xinference", "XunFei", "ZHIPU-AI"}
	for _, provider := range providers {
		t.Run(provider, func(t *testing.T) {
			create := func() ModelDriver {
				d, e := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": "http://unused.invalid"}, URLSuffix{})
				if e != nil || d.Name() == "dummy" {
					t.Fatal(d, e)
				}
				return d
			}
			client := func(d ModelDriver) reflect.Value {
				if f, ok := d.(*FishAudioModel); ok {
					return reflect.ValueOf(f.driver).Elem().FieldByName("httpClient")
				}
				return reflect.ValueOf(d).Elem().FieldByName("httpClient")
			}
			a, b := client(create()), client(create())
			if !a.IsValid() || !b.IsValid() || a.Pointer() == b.Pointer() {
				t.Fatal("missing or shared mutable client")
			}
			at, bt := a.Elem().FieldByName("Transport").Elem(), b.Elem().FieldByName("Transport").Elem()
			if at.IsNil() || bt.IsNil() || at.Pointer() != bt.Pointer() {
				t.Fatal("per-instance connection pool")
			}
		})
	}
}

func TestDummyMethodsNeverSucceed(t *testing.T) {
	d := NewDummyModel(nil, URLSuffix{})
	name, message := "model", "q"
	for _, call := range []func() error{
		func() error {
			_, e := d.ChatWithMessages(name, nil, []Message{{Role: "user", Content: message}}, nil)
			return e
		},
		func() error { _, e := d.Embed(&name, []string{message}, nil, nil); return e },
		func() error { _, e := d.Rerank(&name, message, []string{message}, nil, nil); return e },
		func() error { _, e := d.ListModels(nil); return e },
		func() error { _, e := d.Balance(nil); return e },
		func() error {
			return d.ChatStreamlyWithMessages(name, nil, nil, nil, func(*string, *string) error { t.Error("stub emitted success"); return nil })
		},
	} {
		if err := call(); err == nil || !strings.Contains(err.Error(), "not") && !strings.Contains(err.Error(), "unsupported") {
			t.Fatal(err)
		}
	}
}
