package common

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestReferenceMetadataCanonicalPrecedenceAndOwnership(t *testing.T) {
	for _, fields := range []any{[]any{"author", "date"}, []string{"author", "date"}, []any{}} {
		request := map[string]any{"reference_metadata": map[string]any{"include": false, "fields": fields}}
		config := map[string]any{"reference_metadata": map[string]any{"include": true, "fields": []any{"config"}}}
		before, _ := json.Marshal(request)
		include, got := ResolveReferenceMetadata(request, config)
		want := []string{"author", "date"}
		if reflect.DeepEqual(fields, []any{}) {
			want = []string{}
		}
		if include || !reflect.DeepEqual(got, want) {
			t.Fatal(include, got)
		}
		if len(got) > 0 {
			got[0] = "changed"
		}
		after, _ := json.Marshal(request)
		if string(before) != string(after) {
			t.Fatal("output mutated source")
		}
	}
	include, fields := ResolveReferenceMetadata(map[string]any{"include_metadata": true, "metadata_fields": []any{"legacy"}}, nil)
	if !include || !reflect.DeepEqual(fields, []string{"legacy"}) {
		t.Fatal("frozen key precedence changed")
	}
}
