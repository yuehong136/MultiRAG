package entity

import (
	"encoding/json"
	"reflect"
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
	pm := &ProviderManager{
		Providers: []Provider{{Name: "SiliconFlow", Models: []*Model{&providerOnly}}, {Name: "other", Models: []*Model{&raw}}},
		modelIndex: map[string]*Model{
			"org/raw": &raw, "org/raw@literal": &exact, "org/raw@revision": &versioned,
		},
	}
	names := []string{"org/raw@owner", "provider-only@owner", "org/raw@literal", "org/raw@revision@owner", "unknown@owner", "org/raw@", "@owner"}
	before := append([]string{}, names...)
	rows := pm.DescribeSupportedModels("sILICONfLOW", names)
	encoded, err := json.Marshal(rows)
	want := `[{"name":"org/raw@owner","max_tokens":0,"max_dimension":0,"model_types":[],"thinking":{"default_value":false,"clear_thinking":false}},{"name":"provider-only@owner","max_tokens":32,"model_types":["chat"]},{"name":"org/raw@literal","max_tokens":7},{"name":"org/raw@revision@owner","max_tokens":16},{"name":"unknown@owner"},{"name":"org/raw@"},{"name":"@owner"}]`
	if err != nil || string(encoded) != want || !reflect.DeepEqual(names, before) {
		t.Fatalf("metadata/identity/order = %s, %v; want %s", encoded, err, want)
	}
	*rows[0].MaxTokens, *rows[0].MaxDimension = 99, 99
	rows[0].Thinking.DefaultValue = true
	*rows[1].ModelTypes = append(*rows[1].ModelTypes, "changed")
	again, _ := json.Marshal(pm.DescribeSupportedModels("SiliconFlow", names))
	if string(again) != want {
		t.Fatal("owner fallback changed catalog metadata", string(again))
	}
	other := pm.DescribeSupportedModels("other", []string{"org/raw@owner", "provider-only@owner"})
	if other[0].MaxTokens != nil || other[1].MaxTokens != nil {
		t.Fatal("owner fallback escaped SiliconFlow")
	}
}
