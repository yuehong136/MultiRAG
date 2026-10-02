package infinity

import (
	"strings"
	"testing"
)

func TestDocumentParentFilterLegacyAndScopedReferences(t *testing.T) {
	data := map[string][]interface{}{
		"id":     {"old", "child", "new", "ordinary", "foreign-child"},
		"doc_id": {"doc", "doc", "doc", "doc", "other"},
		"mom_id": {"", "old", "new", "", "ordinary"},
	}
	filter, err := documentParentFilter(data, 5)
	if err != nil || !strings.Contains(filter, "'old'") || !strings.Contains(filter, "'new'") || strings.Contains(filter, "ordinary") || strings.Contains(filter, "other") {
		t.Fatalf("legacy/new parent scope incorrect: %q %v", filter, err)
	}
	if _, err := documentParentFilter(data, 6); err == nil {
		t.Fatal("truncated relationship read accepted")
	}
	data["id"][1] = "old"
	if _, err := documentParentFilter(data, 5); err == nil {
		t.Fatal("duplicate relationship read accepted")
	}
}
