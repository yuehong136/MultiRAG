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
	if a != b || a.Name != "canonical" {
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
