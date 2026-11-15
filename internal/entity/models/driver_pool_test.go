package models

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"reflect"
	"testing"
)

func remainingPoolDrivers(url string) map[string]struct {
	driver ModelDriver
	client *http.Client
} {
	urls := map[string]string{"default": url}
	suffix := URLSuffix{Chat: "chat", Models: "models"}
	a, g, s, z, l := NewAliyunModel(urls, suffix), NewGiteeModel(urls, suffix), NewSiliconflowModel(urls, suffix), NewZhipuAIModel(urls, suffix), NewLmStudioModel(urls, suffix)
	return map[string]struct {
		driver ModelDriver
		client *http.Client
	}{"Aliyun": {a, a.baseModel.httpClient}, "Gitee": {g, g.baseModel.httpClient}, "SiliconFlow": {s, s.baseModel.httpClient}, "ZHIPU-AI": {z, z.baseModel.httpClient}, "lmstudio": {l, l.baseModel.httpClient}}
}

func TestRemainingDriverPoolReuseAndCredentialIsolation(t *testing.T) {
	for _, provider := range []string{"Aliyun", "Gitee", "SiliconFlow", "ZHIPU-AI", "lmstudio"} {
		t.Run(provider, func(t *testing.T) {
			var addresses, keys []string
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				io.Copy(io.Discard, r.Body)
				addresses = append(addresses, r.RemoteAddr)
				keys = append(keys, r.Header.Get("Authorization"))
				fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
			}))
			first, second := remainingPoolDrivers(srv.URL)[provider], remainingPoolDrivers(srv.URL)[provider]
			if first.client == second.client || first.client.Timeout != 0 {
				t.Fatal("pool/client lifetime")
			}
			for i, driver := range []ModelDriver{first.driver, second.driver, first.driver} {
				key := fmt.Sprint(i)
				result, err := driver.ChatWithMessages("served", []Message{{Role: "user", Content: "question"}}, &APIConfig{ApiKey: &key}, nil)
				if err != nil || result == nil || *result.Answer != "answer" {
					t.Fatal(result, err)
				}
			}
			if len(addresses) != 3 || addresses[0] != addresses[2] || !reflect.DeepEqual(keys, []string{"Bearer 0", "Bearer 1", "Bearer 2"}) {
				t.Fatal(addresses, keys)
			}
			ctx, cancel := context.WithCancel(t.Context())
			cancel()
			_, err := first.driver.ChatWithMessages("served", []Message{{Role: "user", Content: "question"}}, &APIConfig{ApiKey: stringPtr("key"), Context: ctx}, nil)
			if !errors.Is(err, context.Canceled) || len(addresses) != 3 {
				t.Fatal("cancellation", err, addresses)
			}
		})
	}
}

func TestRemainingChatStrictFramingAndJSON(t *testing.T) {
	for _, provider := range []string{"lmstudio", "ZHIPU-AI"} {
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
				api := &APIConfig{ApiKey: stringPtr("key")}
				if mode == "bad-json" {
					if r, e := driver.ChatWithMessages("served", []Message{{Role: "user", Content: "q"}}, api, nil); e == nil || r != nil {
						t.Fatal(r, e)
					}
					return
				}
				ctx, cancel := context.WithCancel(t.Context())
				defer cancel()
				api.Context = ctx
				frames := []string{}
				sentinel := errors.New("sender failed")
				err := driver.ChatStreamlyWithSender("served", []Message{{Role: "user", Content: "q"}}, api, nil, func(content, _ *string) error {
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
	// Canonical drivers own distinct clients and cloned standard transports.
	providers := []string{"302.AI", "Aliyun", "Anthropic", "Astraflow", "Avian", "Azure-OpenAI", "Baichuan", "Baidu", "Bedrock", "Cohere", "CometAPI", "DeepInfra", "DeepSeek", "FishAudio", "FuturMix", "Gitee", "GPUStack", "Groq", "HuaweiCloud", "HuggingFace", "HunYuan", "JieKouAI", "Jina", "lmstudio", "LocalAI", "LongCat", "MinerU.Net", "MinerU", "MiniMax", "Mistral", "ModelScope", "Moonshot", "n1n", "Novita", "NVIDIA", "Ollama", "OpenAI", "OpenRouter", "OrcaRouter", "PaddleOCR.Net", "PaddleOCR", "Perplexity", "PPIO", "Qiniu", "Replicate", "SiliconFlow", "StepFun", "TogetherAI", "TokenHub", "TokenPony", "Upstage", "vllm", "VolcEngine", "Voyage", "xAI", "Xiaomi", "Xinference", "XunFei", "ZHIPU-AI"}
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
				base := reflect.ValueOf(d).Elem().FieldByName("baseModel")
				if base.Kind() == reflect.Pointer {
					base = base.Elem()
				}
				return base.FieldByName("httpClient")
			}

			a, b := client(create()), client(create())
			if !a.IsValid() || !b.IsValid() || a.Pointer() == b.Pointer() {
				t.Fatal("missing or shared mutable client")
			}
			at, bt := a.Elem().FieldByName("Transport").Elem(), b.Elem().FieldByName("Transport").Elem()
			if at.IsNil() || bt.IsNil() || at.Pointer() == bt.Pointer() {
				t.Fatal("missing or shared mutable transport")
			}
		})
	}
}

func TestDummyMethodsNeverSucceed(t *testing.T) {
	d := NewDummyModel(nil, URLSuffix{})
	name, message := "model", "q"
	for _, call := range []func() error{
		func() error {
			_, e := d.ChatWithMessages(name, []Message{{Role: "user", Content: message}}, nil, nil)
			return e
		},
		func() error { _, e := d.Embed(&name, []string{message}, nil, nil); return e },
		func() error { _, e := d.Rerank(&name, message, []string{message}, nil, nil); return e },
		func() error { _, e := d.ListModels(nil); return e },
		func() error { _, e := d.Balance(nil); return e },
		func() error {
			return d.ChatStreamlyWithSender(name, nil, nil, nil, func(*string, *string) error { t.Error("stub emitted success"); return nil })
		},
	} {
		if err := call(); err == nil {
			t.Fatal(err)
		}
	}
}
