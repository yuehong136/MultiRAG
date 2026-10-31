package entity

import (
	"encoding/json"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
)

func TestQwenCatalogMetadataAndAliases(t *testing.T) {
	pm := &ProviderManager{}
	if err := pm.LoadAllModels(filepath.Join("..", "..", "configs", "all_models.json")); err != nil {
		t.Fatal(err)
	}
	qwen, deepSeek := 0, 0
	for _, model := range pm.ListAllModels() {
		if strings.HasPrefix(model.Name, "deepseek-") {
			deepSeek++
		}
		if !strings.HasPrefix(model.Name, "qwen/") {
			continue
		}
		qwen++
		want, err := json.Marshal(model)
		if err != nil {
			t.Fatal(err)
		}
		for _, alias := range append([]string{model.Name}, model.Alias...) {
			for _, spelling := range []string{alias, strings.ToLower(alias), strings.ToUpper(alias)} {
				got, err := pm.GetModelByNameOrAlias(spelling)
				if err != nil {
					t.Fatalf("lookup %q: %v", spelling, err)
				}
				raw, err := json.Marshal(got)
				if err != nil || string(raw) != string(want) {
					t.Fatalf("lookup %q changed metadata for %q", spelling, model.Name)
				}
			}
		}
		if slices.Contains(model.ModelTypes, "asr") || slices.Contains(model.ModelTypes, "vision") {
			t.Fatalf("catalog type aliases were not normalized: %q %v", model.Name, model.ModelTypes)
		}
	}
	if qwen != 454 || deepSeek != 70 || len(pm.ListAllModels()) != 530 {
		t.Fatalf("catalog models: Qwen=%d DeepSeek=%d total=%d", qwen, deepSeek, len(pm.ListAllModels()))
	}
	for _, tc := range []struct {
		alias, canonical  string
		tokens, dimension int
		types             []string
		thinking          bool
	}{
		{"Qwen/Qwen3.6-27B", "qwen/qwen3.6-27b", 262144, 0, []string{"chat", "image2text", "video_understanding"}, true},
		{"QWEN3-4B-THINKING-2507", "qwen/qwen3-4b-thinking-2507", 262144, 0, []string{"chat"}, true},
		{"Qwen/QwQ-32B", "qwen/qwq-32b", 131072, 0, []string{"chat"}, true},
		{"Qwen3-Embedding-0.6B", "qwen/qwen3-embedding-0.6b", 32768, 1024, []string{"embedding"}, false},
		{"Qwen3-Embedding-4B", "qwen/qwen3-embedding-4b", 32768, 2560, []string{"embedding"}, false},
		{"Qwen3-Embedding-8B", "qwen/qwen3-embedding-8b", 32768, 4096, []string{"embedding"}, false},
		{"Qwen3-Reranker-8B", "qwen/qwen3-reranker-8b", 32768, 0, []string{"rerank"}, false},
		{"qwen3-asr-1.7b", "qwen/qwen3-asr-1.7b", 65536, 0, []string{"speech2text"}, false},
		{"Qwen/Qwen3-TTS-12Hz-1.7B-Base", "qwen/qwen3-tts-12hz-1.7b-base", 65536, 0, []string{"tts"}, false},
		{"qwen-tokenizer", "qwen/qwen-tokenizer", 0, 0, []string{"tokenizer"}, false},
	} {
		model, err := pm.GetModelByNameOrAlias(tc.alias)
		if err != nil || model.Name != tc.canonical || model.MaxTokens != tc.tokens || !reflect.DeepEqual(model.ModelTypes, tc.types) {
			t.Fatalf("metadata for %q: %#v %v", tc.alias, model, err)
		}
		if tc.dimension == 0 && model.Dimension != nil || tc.dimension != 0 && (model.Dimension == nil || *model.Dimension != tc.dimension) {
			t.Fatalf("dimension for %q: %v", tc.alias, model.Dimension)
		}
		if (model.Thinking != nil) != tc.thinking || tc.thinking && (!model.Thinking.DefaultValue || !model.Thinking.ClearThinking) {
			t.Fatalf("thinking for %q: %#v", tc.alias, model.Thinking)
		}
	}
	glm, err := pm.GetModelByNameOrAlias("GLM-4.5V")
	if err != nil || !reflect.DeepEqual(glm.ModelTypes, []string{"image2text"}) {
		t.Fatalf("existing vision type mapping changed: %#v %v", glm, err)
	}
}

func TestQwenCatalogDiscoveryIdentityAndCopies(t *testing.T) {
	pm := &ProviderManager{}
	if err := pm.LoadAllModels(filepath.Join("..", "..", "configs", "all_models.json")); err != nil {
		t.Fatal(err)
	}
	ids := []string{"Qwen/Qwen3-Embedding-8B", "QWEN3-ASR-1.7B", "Qwen/Qwen-Image", "qwen-tokenizer", "unknown/qwen", "Qwen/Qwen3-Embedding-8B"}
	rows := pm.DescribeSupportedModels("", ids)
	if len(rows) != len(ids) {
		t.Fatal("discovery changed inventory cardinality")
	}
	for i, row := range rows {
		if row.Name != ids[i] {
			t.Fatalf("discovery rewrote remote ID %q to %q", ids[i], row.Name)
		}
	}
	if rows[0].MaxTokens == nil || *rows[0].MaxTokens != 32768 || rows[0].Dimension == nil || *rows[0].Dimension != 4096 || rows[1].MaxTokens == nil || *rows[1].MaxTokens != 65536 {
		t.Fatalf("discovered metadata missing: %#v", rows)
	}
	// Image generation and tokenizer tags describe catalog metadata. They do not
	// register provider models or promise an implemented inference interface.
	if rows[2].MaxTokens != nil || rows[3].MaxTokens != nil || rows[4].ModelTypes != nil || rows[4].Thinking != nil || rows[4].MaxTokens != nil {
		t.Fatal("discovery invented omitted metadata")
	}
	before := pm.DescribeSupportedModels("", ids)
	*rows[0].MaxTokens = 1
	*rows[0].Dimension = 1
	(*rows[0].ModelTypes)[0] = "changed"
	listed := pm.ListAllModels()
	for _, model := range listed {
		if model.Name == "qwen/qwen3.6-27b" {
			model.Alias[0] = "changed"
			model.Thinking.DefaultValue = false
			model.ModelTypes[0] = "changed"
		}
	}
	if !reflect.DeepEqual(before, pm.DescribeSupportedModels("", ids)) {
		t.Fatal("consumer mutated published catalog")
	}
	model, err := pm.GetModelByNameOrAlias("Qwen/Qwen3.6-27B")
	if err != nil || !model.Thinking.DefaultValue || model.ModelTypes[0] != "chat" {
		t.Fatal("list exposed mutable catalog metadata")
	}
}
