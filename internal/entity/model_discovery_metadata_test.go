package entity

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"multirag/internal/testsupport"
)

func TestSupportedModelMetadataPresenceAndIsolation(t *testing.T) {
	journal := testsupport.NewCase(t)
	path := filepath.Join(t.TempDir(), "catalog.json")
	journal.Event("reserve-catalog", map[string]any{"path": path})
	raw := `[{"name":"canonical","alias":["namespace/raw"],"max_tokens":42,"dimension":128,"model_types":["embedding"],"thinking":{"default_value":false,"clear_thinking":false}}, {"name":"zero","max_tokens":0,"dimension":0,"model_types":[]}, {"name":"absent"}, {"name":"null","max_tokens":null,"model_types":null,"dimension":null,"thinking":null}]`
	if err := os.WriteFile(path, []byte(raw), 0600); err != nil {
		t.Fatal(err)
	}
	pm := &ProviderManager{}
	if err := pm.LoadAllModels(path); err != nil {
		t.Fatal(err)
	}
	names := []string{"namespace/raw", "unknown", "canonical", "zero", "absent", "null"}
	rows := pm.DescribeSupportedModels("unknown-provider", names)
	got, err := json.Marshal(rows)
	if err != nil {
		t.Fatal(err)
	}
	expected := `[{"name":"namespace/raw","max_tokens":42,"dimension":128,"model_types":["embedding"],"thinking":{"default_value":false,"clear_thinking":false}},{"name":"unknown"},{"name":"canonical","max_tokens":42,"dimension":128,"model_types":["embedding"],"thinking":{"default_value":false,"clear_thinking":false}},{"name":"zero","max_tokens":0,"dimension":0,"model_types":[]},{"name":"absent"},{"name":"null"}]`
	if string(got) != expected {
		t.Fatalf("metadata presence/order/cardinality: %s", got)
	}
	*rows[0].MaxTokens = 99
	*rows[0].Dimension = 99
	(*rows[0].ModelTypes)[0] = "chat"
	rows[0].Thinking.DefaultValue = true
	if rows[2].Thinking.DefaultValue || *rows[2].Dimension != 128 || *rows[2].MaxTokens != 42 || (*rows[2].ModelTypes)[0] != "embedding" {
		t.Fatal("rows share metadata")
	}
	again := pm.DescribeSupportedModels("unknown-provider", names)
	encoded, _ := json.Marshal(again)
	if string(encoded) != expected {
		t.Fatalf("catalog mutated: %s", encoded)
	}
	empty, _ := json.Marshal(pm.DescribeSupportedModels("unknown-provider", nil))
	if string(empty) != "[]" {
		t.Fatalf("empty inventory: %s", empty)
	}
	journal.Event("metadata-contract", map[string]any{"rows": len(rows), "rawID": rows[0].Name, "zeroPreserved": true, "emptyTypesPreserved": true, "deepCopy": true})
}

func TestSupportedMetadataTypedDecodeAndFailedPublication(t *testing.T) {
	journal := testsupport.NewCase(t)
	path := filepath.Join(t.TempDir(), "catalog.json")
	journal.Event("reserve-catalog", map[string]any{"path": path})
	pm := &ProviderManager{}
	if err := os.WriteFile(path, []byte(`[{"name":"kept","max_tokens":0}]`), 0600); err != nil {
		t.Fatal(err)
	}
	if err := pm.LoadAllModels(path); err != nil {
		t.Fatal(err)
	}
	before := pm.DescribeSupportedModels("", []string{"kept"})
	for _, raw := range []string{`[{"name":"bad","max_tokens":"0"}]`, `[{"name":"bad","dimension":"128"}]`, `[{"name":"bad","model_types":false}]`, `[{"name":"bad","thinking":false}]`, `[{"name":"dup","alias":["dup"]}]`, `[{"name":"bad"},null]`, `broken`} {
		if err := os.WriteFile(path, []byte(raw), 0600); err != nil {
			t.Fatal(err)
		}
		if err := pm.LoadAllModels(path); err == nil {
			t.Fatalf("accepted %s", raw)
		}
		if !reflect.DeepEqual(before, pm.DescribeSupportedModels("", []string{"kept"})) {
			t.Fatal("partially published invalid catalog")
		}
	}
}

func TestSupportedMetadataProviderFallback(t *testing.T) {
	var model Model
	if err := json.Unmarshal([]byte(`{"name":"provider-only","max_tokens":0,"model_types":[]}`), &model); err != nil {
		t.Fatal(err)
	}
	pm := &ProviderManager{Providers: []Provider{{Name: "Gitee", Models: []*Model{&model}}}}
	rows := pm.DescribeSupportedModels("Gitee", []string{"unknown-alias", "provider-only"})
	if rows[0].MaxTokens != nil || rows[0].ModelTypes != nil || rows[1].MaxTokens == nil || *rows[1].MaxTokens != 0 || rows[1].ModelTypes == nil || len(*rows[1].ModelTypes) != 0 {
		t.Fatalf("provider fallback: %#v", rows)
	}
}
