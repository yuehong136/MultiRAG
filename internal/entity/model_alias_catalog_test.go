package entity

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/google/uuid"
	"multirag/internal/testsupport"
)

func TestCatalogCaseInsensitiveLookupAndAtomicCollisions(t *testing.T) {
	j := testsupport.NewCase(t)
	path := filepath.Join(os.TempDir(), "multirag-alias-"+uuid.NewString()+".json")
	j.Event("reserve-catalog", map[string]any{"path": path})
	t.Cleanup(func() {
		if err := os.Remove(path); err != nil {
			t.Error(err)
		}
		_, err := os.Stat(path)
		j.Event("cleanup-catalog", map[string]any{"path": path, "absent": os.IsNotExist(err)})
	})
	write := func(raw string) {
		t.Helper()
		if err := os.WriteFile(path, []byte(raw), 0600); err != nil {
			t.Fatal(err)
		}
	}
	write(`[{"name":"DeepSeek-Canonical","alias":["deepseek-ai/DeepSeek-Alias"],"max_tokens":0,"model_types":[],"thinking":{"default_value":false,"clear_thinking":false}}]`)
	pm := &ProviderManager{}
	if err := pm.LoadAllModels(path); err != nil {
		t.Fatal(err)
	}
	names := []string{"DEEPSEEK-CANONICAL", "DeepSeek-AI/deepseek-alias", "unknown"}
	before := pm.DescribeSupportedModels("", names)
	if before[0].Name != names[0] || before[1].Name != names[1] || before[1].MaxTokens == nil || *before[1].MaxTokens != 0 || before[1].ModelTypes == nil || len(*before[1].ModelTypes) != 0 || before[1].Thinking == nil || before[1].Thinking.DefaultValue || before[2].MaxTokens != nil {
		t.Fatalf("case lookup changed raw IDs or presence: %#v", before)
	}
	for _, raw := range []string{
		`[{"name":"one","alias":["Namespace/Shared"]},{"name":"two","alias":["namespace/shared"]}]`,
		`[{"name":"one","alias":["TWO"]},{"name":"two"}]`,
		`[{"name":"one"},{"name":"ONE"}]`,
		`[{"name":"one","alias":["alias","ALIAS"]}]`,
		`[{"name":"one","alias":["ONE"]}]`,
		`[{"name":"one","alias":["one"]}]`,
	} {
		write(raw)
		if err := pm.LoadAllModels(path); err == nil {
			t.Fatalf("accepted ambiguous or redundant catalog: %s", raw)
		}
		if !reflect.DeepEqual(before, pm.DescribeSupportedModels("", names)) || pm.ListAllModels()[0].Name != "DeepSeek-Canonical" {
			t.Fatal("failed load replaced published metadata")
		}
	}
	model, err := pm.GetModelByNameOrAlias(names[1])
	if err != nil || model.Name != "DeepSeek-Canonical" || model.Alias[0] != "deepseek-ai/DeepSeek-Alias" {
		t.Fatal("lookup rewrote catalog identity", model, err)
	}
	model.Alias[0] = "changed"
	model.Thinking.DefaultValue = true
	model.MaxTokens = 9
	if !reflect.DeepEqual(before, pm.DescribeSupportedModels("", names)) {
		t.Fatal("lookup exposed mutable catalog metadata")
	}
	j.Event("alias-contract", map[string]any{"caseInsensitive": true, "crossModelCollisionsRejected": true, "sameModelDuplicatesRejected": true, "failedPublicationAtomic": true, "rawIDsPreserved": true})
}

func TestDeepSeekCatalogMetadataAndAliases(t *testing.T) {
	pm := &ProviderManager{}
	if err := pm.LoadAllModels(filepath.Join("..", "..", "configs", "all_models.json")); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name, alias, kind string
		tokens            int
		thinking          bool
	}{
		{"deepseek-v4-flash", "DEEPSEEK-CHAT", "chat", 1048576, true},
		{"deepseek-v4-pro", "DeepSeek-AI/DeepSeek-V4-Pro", "chat", 1048576, true},
		{"deepseek-r1", "DeepSeek-AI/DEEPSEEK-R1", "chat", 163840, false},
		{"deepseek-r1-distill-qwen-32b", "DEEPSEEK-R1-DISTILL-QWEN-32B-250120", "chat", 131072, false},
		{"deepseek-ocr-2", "DEEPSEEK-AI/DEEPSEEK-OCR-2", "ocr", 8192, false},
		{"deepseek-vl2-tiny", "DEEPSEEK-AI/DEEPSEEK-VL2-TINY", "image2text", 4096, false},
		{"deepseek-coder-6.7b-base", "DEEPSEEK-AI/DEEPSEEK-CODER-6.7B-BASE", "chat", 16384, false},
	} {
		model, err := pm.GetModelByNameOrAlias(tc.alias)
		if err != nil || model.Name != tc.name || model.MaxTokens != tc.tokens || !reflect.DeepEqual(model.ModelTypes, []string{tc.kind}) || (model.Thinking != nil) != tc.thinking {
			t.Fatalf("alias %q: %#v %v", tc.alias, model, err)
		}
	}
	count := 0
	for _, model := range pm.ListAllModels() {
		if !strings.HasPrefix(model.Name, "deepseek-") {
			continue
		}
		count++
		for _, alias := range append([]string{model.Name}, model.Alias...) {
			for _, spelling := range []string{alias, strings.ToLower(alias), strings.ToUpper(alias)} {
				got, err := pm.GetModelByNameOrAlias(spelling)
				want, _ := json.Marshal(model)
				data, _ := json.Marshal(got)
				if err != nil || string(data) != string(want) {
					t.Fatalf("alias %q did not resolve %q: %v", spelling, model.Name, err)
				}
			}
		}
	}
	if count != 70 {
		t.Fatalf("DeepSeek catalog entries = %d, want 70", count)
	}
}
