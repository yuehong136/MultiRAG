package milvus

import (
	"multirag/internal/engine/types"
	"strings"
	"testing"
)

func TestKGSearchRemainsExplicitlyUnsupported(t *testing.T) {
	store := &milvusEngine{}
	result, err := store.Search(t.Context(), &types.SearchRequest{IndexNames: []string{"multirag_owned"}, KbIDs: []string{"owned"}, SelectFields: []string{"entity_kwd"}, Filter: map[string]any{"knowledge_graph_kwd": "entity"}, Limit: 1})
	if err == nil || !strings.Contains(err.Error(), "not yet implemented") || result != nil {
		t.Fatal("unsupported search claimed success", result, err)
	}
}
