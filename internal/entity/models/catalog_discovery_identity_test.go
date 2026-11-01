package models

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"reflect"
	"sync/atomic"
	"testing"
)

// Keep all provider implementations represented by the discovery refactor on
// their configured wire formats while catalog enrichment remains in the service.
func TestCatalogDiscoveryProviderIDsHTTP(t *testing.T) {
	providers := []string{
		"302.AI", "Aliyun", "Astraflow", "Avian", "Azure-OpenAI", "Baidu", "Cohere", "CometAPI", "DeepInfra", "DeepSeek", "Gitee", "Google", "GPUStack", "Groq", "HuaweiCloud", "HuggingFace", "Hunyuan", "JieKouAI", "Jina", "LM-Studio", "LocalAI", "LongCat", "Minimax", "Mistral", "ModelScope", "Moonshot", "n1n", "Novita", "NVIDIA", "OpenAI", "OpenRouter", "OrcaRouter", "Perplexity", "PPIO", "Qiniu", "Replicate", "SiliconFlow", "StepFun", "TogetherAI", "TokenHub", "TokenPony", "Upstage", "vllm", "VolcEngine", "xAI", "Xinference", "XunFei", "ZHIPU-AI",
	}
	for _, provider := range providers {
		t.Run(provider, func(t *testing.T) {
			for _, empty := range []bool{false, true} {
				var items []map[string]string
				want := []string{}
				if !empty {
					items = []map[string]string{{"id": "namespace/second@revision", "owned_by": "owner"}, {"id": "namespace/first", "owned_by": "owner"}}
					want = []string{"namespace/second@revision", "namespace/first"}
				} else {
					items = []map[string]string{}
				}
				var payload any = map[string]any{"data": items}
				switch provider {
				case "Avian", "TogetherAI":
					payload = items
				case "Aliyun", "Cohere", "DeepInfra", "Google", "Replicate":
					native := make([]map[string]string, 0, len(items))
					for i, item := range items {
						name := item["id"]
						switch provider {
						case "Aliyun":
							native = append(native, map[string]string{"model_name": name})
						case "Cohere":
							native = append(native, map[string]string{"name": name})
						case "DeepInfra":
							native = append(native, map[string]string{"model_name": name})
						case "Google":
							native = append(native, map[string]string{"name": name, "displayName": "display label"})
						case "Replicate":
							name = []string{"second", "first"}[i]
							native = append(native, map[string]string{"owner": "owner", "name": name})
							want[i] = "owner/" + name
						}
					}
					switch provider {
					case "Aliyun":
						payload = map[string]any{"output": map[string]any{"models": native}}
					case "Cohere", "Google":
						payload = map[string]any{"models": native}
					case "DeepInfra":
						payload = native
					case "Replicate":
						payload = map[string]any{"results": native}
					}
				case "SiliconFlow":
					for i := range want {
						want[i] += "@owner"
					}
				case "XunFei":
					want = []string{"spark-x"}
				}
				var calls atomic.Int32
				server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					calls.Add(1)
					if r.Method != http.MethodGet {
						t.Error("discovery must use GET")
					}
					if err := json.NewEncoder(w).Encode(payload); err != nil {
						t.Error(err)
					}
				}))
				driver, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Models: "models"})
				if err != nil {
					t.Fatal(err)
				}
				api := &APIConfig{APIKey: stringPtr("fixture")}
				got, err := driver.ListModels(api)
				if err != nil || !reflect.DeepEqual(got, want) {
					t.Fatalf("provider=%s empty=%v: got %#v, %v; want %#v", provider, empty, got, err, want)
				}
				beforeCancel := calls.Load()
				ctx, cancel := context.WithCancel(t.Context())
				cancel()
				if names, err := driver.ListModels(&APIConfig{APIKey: api.APIKey, Context: ctx}); !errors.Is(err, context.Canceled) || names != nil || calls.Load() != beforeCancel {
					t.Fatal("cancelled discovery published inventory or reached HTTP", names, err)
				}
				if provider == "XunFei" && beforeCancel != 0 || provider != "XunFei" && beforeCancel != 1 {
					t.Fatal("discovery made unexpected provider calls", beforeCancel)
				}
				providerResourceRecord(t, map[string]any{"action": "catalog-provider-identity", "provider": provider, "empty": empty, "names": got, "cancelBeforeHTTP": true, "providerCalls": beforeCancel})
			}
		})
	}
}
