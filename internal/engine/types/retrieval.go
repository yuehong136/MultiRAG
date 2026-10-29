package types

import (
	"encoding/json"
	"fmt"
	"math/big"
	"strings"
)

// SearchKind identifies internal search capabilities that clients cannot set.
type SearchKind uint8

const (
	SearchKindDefault SearchKind = iota
	SearchKindKnowledgeGraph
)

// ExplicitVisibility permits disabled graph records only for a scoped internal
// graph request. An ordinary client filter remains intersected with availability.
func (r *SearchRequest) ExplicitVisibility() (bool, error) {
	if r.Kind == SearchKindDefault {
		return r.IncludeUnavailable, nil
	}
	if r.Kind != SearchKindKnowledgeGraph {
		return false, fmt.Errorf("unsupported internal search kind")
	}
	if r.IncludeUnavailable || len(r.IndexNames) != 1 || strings.TrimSpace(r.IndexNames[0]) == "" || len(r.KbIDs) != 1 || strings.TrimSpace(r.KbIDs[0]) == "" {
		return false, fmt.Errorf("graph search requires one explicit index and dataset scope")
	}
	kind, ok := r.Filter["knowledge_graph_kwd"].(string)
	if !ok || (kind != "entity" && kind != "relation" && kind != "community_report" && kind != "ty2ents") {
		return false, fmt.Errorf("graph search requires an exact graph record kind")
	}
	values, list, err := ConditionValues(r.Filter["available_int"])
	if err != nil || list || len(values) != 1 {
		return false, fmt.Errorf("graph search requires available_int zero")
	}
	raw, err := json.Marshal(values[0])
	zero, numeric := new(big.Rat).SetString(string(raw))
	if err != nil || !numeric || zero.Sign() != 0 {
		return false, fmt.Errorf("graph search requires available_int zero")
	}
	return true, nil
}

// RerankWindow keeps block fetching and in-block slicing aligned to full pages.
// Top limits candidate selection; rounding the block does not enlarge that pool.
func RerankWindow(pageSize, top int) int {
	if pageSize <= 1 {
		if top > 0 && top < 30 {
			return top
		}
		return 30
	}
	window := pageSize
	if pageSize < 64 {
		pages := 64 / pageSize
		if 64%pageSize != 0 {
			pages++
		}
		window = pages * pageSize
	}
	if top > 0 && top < window {
		pages := top / pageSize
		if top%pageSize != 0 {
			pages++
		}
		window = pages * pageSize
	}
	return window
}
