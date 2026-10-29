package entity

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestAfter7ddStrictSuffix(t *testing.T) {
	for _, suffix := range []string{`{"embedding":"embed","asr":"stt"}`, `null`, `{}`, `{"embeddings":"embed"}`, `[]`, `"chat"`} {
		var p Provider
		err := json.Unmarshal([]byte(`{"name":"x","extension":true,"url_suffix":`+suffix+`}`), &p)
		valid := suffix == `null` || suffix == `{}` || suffix == `{"embedding":"embed","asr":"stt"}`
		if (err == nil) != valid {
			t.Fatalf("%s: %v", suffix, err)
		}
	}
}
func TestAfter7ddCatalogLookupAtomic(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "catalog.json")
	write := func(s string) {
		t.Helper()
		if err := os.WriteFile(path, []byte(s), 0600); err != nil {
			t.Fatal(err)
		}
	}
	var pm ProviderManager
	if _, err := pm.GetModelByNameOrAlias("missing"); err == nil {
		t.Fatal("empty lookup")
	}
	write(`[{"name":"canonical","alias":["alias"],"model_types":["chat"]}]`)
	if err := pm.LoadAllModels(path); err != nil {
		t.Fatal(err)
	}
	a, _ := pm.GetModelByNameOrAlias("canonical")
	b, _ := pm.GetModelByNameOrAlias("alias")
	if a == b || a.Name != "canonical" || b.Name != "canonical" {
		t.Fatal(a, b)
	}
	if _, err := pm.GetModelByNameOrAlias("missing"); err == nil {
		t.Fatal("unknown matched")
	}
	write(`[{"name":"canonical","alias":["x"]},{"name":"x"}]`)
	if err := pm.LoadAllModels(path); err == nil {
		t.Fatal("collision accepted")
	}
	if _, err := pm.GetModelByNameOrAlias("alias"); err != nil {
		t.Fatal("failed load published", err)
	}
	write(`[]`)
	if err := pm.LoadAllModels(path); err != nil {
		t.Fatal(err)
	}
	if len(pm.ListAllModels()) != 0 {
		t.Fatal("empty catalog")
	}
}

func TestAfter7ddManagerRejectsUnknownSuffixBeforePublish(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "provider.json")
	if err := os.WriteFile(path, []byte(`{"name":"DeepSeek","url":{"default":"https://example.invalid"},"extension":true,"url_suffix":{"embeddings":"bad"},"models":[]}`), 0600); err != nil {
		t.Fatal(err)
	}
	if manager, err := NewProviderManager(dir); err == nil || manager != nil {
		t.Fatal(manager, err)
	}
	if err := os.WriteFile(path, []byte(`{"name":"DeepSeek","url":{"default":"https://example.invalid"},"extension":true,"url_suffix":{"embedding":"embed"},"models":[]}`), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := NewProviderManager(dir); err != nil {
		t.Fatal("provider extensions rejected", err)
	}
}

func TestAfter7ddCatalogReturnsIndependentMetadata(t *testing.T) {
	path := filepath.Join(t.TempDir(), "models.json")
	raw := `[{"name":"m","alias":["a"],"model_types":["chat"],"ModelTypeMap":{"chat":true},"class":"qwen3","thinking":{"default_value":true},"features":{"multimodal":{"enabled":true,"input_modalities":["text"]},"reasoning":{"type":"effort","enabled":true,"options":["high"]}}}]`
	if err := os.WriteFile(path, []byte(raw), 0600); err != nil {
		t.Fatal(err)
	}
	var pm ProviderManager
	if err := pm.LoadAllModels(path); err != nil {
		t.Fatal(err)
	}
	mutate := func(m *Model) {
		m.Name = "changed"
		m.Alias[0] = "bad"
		m.ModelTypes[0] = "bad"
		m.ModelTypeMap["chat"] = false
		*m.Class = "bad"
		m.Thinking.DefaultValue = false
		m.Features.Multimodal.InputModalities[0] = "bad"
		m.Features.Reasoning.Effort.Options[0] = "bad"
	}
	mutate(pm.ListAllModels()[0])
	got, err := pm.GetModelByNameOrAlias("a")
	if err != nil {
		t.Fatal(err)
	}
	mutate(got)
	actual, _ := pm.GetModelByNameOrAlias("m")
	if actual.Name != "m" || actual.Alias[0] != "a" || actual.ModelTypes[0] != "chat" || !actual.ModelTypeMap["chat"] || *actual.Class != "qwen3" || !actual.Thinking.DefaultValue || actual.Features.Multimodal.InputModalities[0] != "text" || actual.Features.Reasoning.Effort.Options[0] != "high" {
		t.Fatalf("catalog mutated: %#v", actual)
	}
}

func TestAfter7ddStrictSuffixCaseAliases(t *testing.T) {
	for _, key := range []string{"url_suffix", "URL_SUFFIX", "Url_Suffix"} {
		var provider Provider
		err := json.Unmarshal([]byte(`{"name":"DeepSeek","`+key+`":{"embeddings":"typo"}}`), &provider)
		if err == nil {
			t.Fatalf("case alias %s bypassed strict suffix validation", key)
		}
	}
}
