package handler_test

import (
	"fmt"
	"multirag/internal/cli"
	"multirag/internal/testsupport"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
)

func TestHostedProviderRoutesScratchPostgres(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	for _, provider := range []string{"Mistral", "Jina", "Novita", "LongCat"} {
		t.Run(provider, func(t *testing.T) { exerciseCanonicalChat(t, f, provider, "served", "one") })
	}
}

func TestAdditionalCanonicalProviderConsumersHTTPCLIAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	for _, tc := range []struct{ provider, model string }{
		{"HuaweiCloud", "DeepSeek-V3"}, {"ModelScope", "tenant-chat"}, {"OrcaRouter", "orcarouter/auto"},
		{"Qiniu", "qwen3-32b"}, {"CoHere", "command-r7b-12-2024"}, {"StepFun", "step-2-16k"}, {"Baichuan", "Baichuan4"},
	} {
		t.Run(tc.provider+"/chat", func(t *testing.T) {
			for _, instance := range []string{"one", "two"} {
				t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, tc.provider, tc.model, instance) })
			}
		})
	}
	for _, tc := range []struct{ provider, model, kind string }{
		{"HuaweiCloud", "bge-m3", "embedding"}, {"HuaweiCloud", "bge-reranker-v2-m3", "rerank"},
		{"CoHere", "embed-v4.0", "embedding"}, {"CoHere", "rerank-v3.5", "rerank"},
		{"Baichuan", "Baichuan-Text-Embedding", "embedding"}, {"Upstage", "solar-embedding-1-large-query", "embedding"},
	} {
		t.Run(tc.provider+"/"+tc.kind, func(t *testing.T) {
			for _, instance := range []string{"one", "two"} {
				t.Run(instance, func(t *testing.T) { exerciseCanonicalInference(t, f, tc.provider, tc.model, instance, tc.kind) })
			}
		})
	}
	for _, tc := range []struct{ provider, model, kind string }{
		{"OrcaRouter", "openai/tts-1", "tts"}, {"StepFun", "step-tts-2", "tts"}, {"CoHere", "cohere-transcribe-03-2026", "speech2text"},
	} {
		t.Run(tc.provider+"/"+tc.kind, func(t *testing.T) {
			for _, instance := range []string{"one", "two"} {
				t.Run(instance, func(t *testing.T) { exerciseCanonicalMedia(t, f, tc.provider, tc.model, instance, tc.kind) })
			}
		})
	}
}

func TestRoot55CanonicalProviderConsumersHTTPCLIAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	for _, tc := range []struct{ provider, model string }{
		{"VolcEngine", "doubao-seed-2-0-pro-260215"}, {"TokenHub", "gpt-4o-mini"}, {"xAI", "grok-4"}, {"LongCat", "LongCat-Flash-Chat"}, {"NVIDIA", "meta/llama-3.1-8b-instruct"}, {"HuggingFace", "tenant-chat"}, {"GPUStack", "tenant-chat"}, {"Azure-OpenAI", "tenant-chat"}, {"n1n", "gpt-4o-mini"}, {"lmstudio", "tenant-chat"}, {"MiniMax", "MiniMax-M2.1"}, {"Baidu", "deepseek-v3.2"},
	} {
		t.Run(tc.provider+"/chat", func(t *testing.T) {
			for _, instance := range []string{"one", "two"} {
				t.Run(instance, func(t *testing.T) { exerciseCanonicalChat(t, f, tc.provider, tc.model, instance) })
			}
		})
	}
	for _, tc := range []struct{ provider, model, kind string }{
		{"VolcEngine", "doubao-embedding-vision-250615", "embedding"}, {"NVIDIA", "nvidia/nv-rerankqa-mistral-4b-v3", "rerank"}, {"Gitee", "BAAI/bge-reranker-v2-m3", "rerank"}, {"Gitee", "BAAI/bge-m3", "embedding"},
		{"Aliyun", "text-embedding-v3", "embedding"}, {"Aliyun", "gte-rerank-v2", "rerank"}, {"Bedrock", "cohere.embed-english-v3", "embedding"},
		{"HuggingFace", "sentence-transformers/all-MiniLM-L6-v2", "embedding"}, {"GPUStack", "tenant-embed", "embedding"}, {"Azure-OpenAI", "tenant-embed", "embedding"},
		{"n1n", "text-embedding-3-small", "embedding"}, {"n1n", "BAAI/bge-reranker-v2-m3", "rerank"}, {"Baidu", "embedding-v1", "embedding"}, {"Baidu", "qwen3-reranker-4b", "rerank"},
		{"Mistral", "mistral-embed", "embedding"}, {"Jina", "jina-embeddings-v3", "embedding"}, {"Jina", "jina-reranker-v3", "rerank"},
		{"TogetherAI", "intfloat/multilingual-e5-large-instruct", "embedding"}, {"Xinference", "tenant-embed", "embedding"}, {"Xinference", "tenant-rerank", "rerank"}, {"Novita", "baai/bge-reranker-v2-m3", "rerank"},
		{"Replicate", "yxzwayne/bge-reranker-v2-m3:7f7c6e9d18336e2cbf07d88e9362d881d2fe4d6a9854ec1260f115cabc106a8c", "rerank"},
	} {
		t.Run(tc.provider+"/"+tc.kind, func(t *testing.T) {
			for _, instance := range []string{"one", "two"} {
				t.Run(instance, func(t *testing.T) { exerciseCanonicalInference(t, f, tc.provider, tc.model, instance, tc.kind) })
			}
		})
	}
}

func TestRoot55UnsupportedHostedCapabilitiesHTTPCLIAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	var calls atomic.Int64
	supplier := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		t.Error("unsupported native method contacted supplier")
	}))
	for _, tc := range []struct{ provider, kind string }{{"Mistral", "asr"}, {"Mistral", "tts"}, {"LongCat", "embedding"}, {"LongCat", "rerank"}, {"Jina", "tts"}, {"HuggingFace", "rerank"}} {
		t.Run(tc.provider+"/"+tc.kind, func(t *testing.T) {
			id := f.enroll(t, tc.provider, "unsupported-"+tc.kind, "private-key", supplier.URL, "unsupported-model", tc.kind)
			before := f.snapshot(t)
			body := map[string]any{"model_id": id}
			path, syntax := "", ""
			switch tc.kind {
			case "asr":
				path = "/api/v1/audio/transcriptions"
				body["file"] = "UklGRiYAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAACABAAZGF0YQIAAAABgA=="
				syntax = ""
			case "tts":
				path = "/api/v1/audio/speech"
				body["text"] = "speak"
				syntax = fmt.Sprintf("TTS WITH '%s' TEXT 'speak';", id)
			case "embedding":
				path = "/api/v1/embeddings"
				body["texts"] = []string{"q"}
				syntax = fmt.Sprintf("EMBED TEXT 'q' WITH '%s';", id)
			case "rerank":
				path = "/api/v1/rerank"
				body["query"] = "q"
				body["documents"] = []string{"a"}
				syntax = fmt.Sprintf("RERANK QUERY 'q' DOCUMENT 'a' WITH '%s' TOP 1;", id)
			}
			_, failed, _ := f.request(t, "owner", "POST", path, body)
			if providerEnvelopeError(failed) == nil || !strings.Contains(fmt.Sprint(failed["message"]), "no such method") {
				t.Fatal("unsupported native HTTP capability did not reach original method", failed["message"])
			}
			if syntax != "" {
				cmd, err := cli.NewParser(syntax).Parse(cli.APIMode)
				if err != nil {
					t.Fatal(err)
				}
				if _, err = f.cli(t, "owner").ExecuteCommand(cmd); err == nil {
					t.Fatal("unsupported CLI capability succeeded")
				}
			}
			if f.snapshot(t) != before || calls.Load() != 0 {
				t.Fatal("unsupported operation had SQL/provider effects")
			}
		})
	}
}
