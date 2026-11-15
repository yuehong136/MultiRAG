package models

import (
	"encoding/json"
	"testing"
)

func TestSupportedSiliconFlowOwnerSuffixMetadata(t *testing.T) {
	var raw, providerOnly, exact, versioned Model
	for target, text := range map[*Model]string{
		&raw:          `{"name":"org/raw","max_tokens":0,"max_dimension":0,"model_types":[],"thinking":{"default_value":false,"clear_thinking":false}}`,
		&providerOnly: `{"name":"provider-only","max_tokens":32,"model_types":["chat"]}`,
		&exact:        `{"name":"org/raw@literal","max_tokens":7}`,
		&versioned:    `{"name":"org/raw@revision","max_tokens":16}`,
	} {
		if err := json.Unmarshal([]byte(text), target); err != nil {
			t.Fatal(err)
		}
	}
	before := providerManager
	t.Cleanup(func() { providerManager = before })
	providerManager = &ProviderManager{Providers: []Provider{{Name: "SiliconFlow", Models: []*Model{&providerOnly}}}, AllModels: []Model{raw, exact, versioned}, Alias2ModelIndex: map[string]int{"org/raw": 0, "org/raw@literal": 1, "org/raw@revision": 2}}
	list := ModelList{Models: []DSModel{{ID: "org/raw", OwnedBy: "owner"}, {ID: "provider-only", OwnedBy: "owner"}, {ID: "org/raw@literal"}, {ID: "org/raw@revision", OwnedBy: "owner"}, {ID: "unknown", OwnedBy: "owner"}, {ID: "org/raw@"}, {ID: "@owner"}}}
	rows := ParseListModel(list)
	want := []string{"org/raw@owner", "provider-only@owner", "org/raw@literal", "org/raw@revision@owner", "unknown@owner", "org/raw@", "@owner"}
	if len(rows) != len(want) {
		t.Fatal(rows)
	}
	for i, name := range want {
		if rows[i].Name != name {
			t.Fatal("identity/order", rows)
		}
	}
	if rows[0].MaxTokens == nil || *rows[0].MaxTokens != 0 || rows[0].MaxDimension == nil || *rows[0].MaxDimension != 0 || rows[0].Thinking == nil || rows[0].Thinking.DefaultValue || rows[0].ModelTypes == nil {
		t.Fatal("explicit zero/false/empty metadata lost", rows[0])
	}
	if rows[1].MaxTokens != nil || rows[4].MaxTokens != nil {
		t.Fatal("provider-only fallback is not part of canonical global catalog", rows)
	}
	if *rows[2].MaxTokens != 7 || *rows[3].MaxTokens != 16 {
		t.Fatal("exact/revision identity lost")
	}
	*rows[0].MaxTokens = 99
	*rows[0].MaxDimension = 99
	rows[0].Thinking.DefaultValue = true
	rows[0].ModelTypes = append(rows[0].ModelTypes, "changed")
	again := ParseListModel(list)
	if *again[0].MaxTokens != 0 || *again[0].MaxDimension != 0 || again[0].Thinking.DefaultValue || len(again[0].ModelTypes) != 0 {
		t.Fatal("response mutated global catalog")
	}
	if list.Models[0].ID != "org/raw" || list.Models[0].OwnedBy != "owner" {
		t.Fatal("input list changed")
	}
}
