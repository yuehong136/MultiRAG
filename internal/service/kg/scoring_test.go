package kg

import (
	"encoding/csv"
	"math"
	"reflect"
	"strings"
	"testing"
)

func TestKGAnalyzePathsDeterministicAndFinite(t *testing.T) {
	entities := map[string]*KGEntity{
		"A":   {Sim: 0.9, NhopEnts: []NhopEntity{{Path: []string{"A", "B", "C"}, Weights: []float64{0.8, 0.5}}}},
		"B":   {Sim: 0.6, NhopEnts: []NhopEntity{{Path: []string{"A", "B"}, Weights: []float64{0.2}}}},
		"nil": nil,
		"bad": {Sim: math.NaN(), NhopEnts: []NhopEntity{{Path: []string{"A", "B"}}}},
	}
	want := map[Edge]EdgeScore{{"A", "B"}: {0.75, 0.8}, {"B", "C"}: {0.3, 0.5}}
	for i := 0; i < 100; i++ {
		if got := AnalyzeNHopPaths(entities); !reflect.DeepEqual(got, want) {
			t.Fatalf("path aggregation = %#v, want %#v", got, want)
		}
	}
	if len(AnalyzeNHopPaths(nil)) != 0 {
		t.Fatal("nil paths should be empty")
	}
	zero := AnalyzeNHopPaths(map[string]*KGEntity{"zero": {NhopEnts: []NhopEntity{{Path: []string{"X", "Y"}, Weights: []float64{0}}}}})
	if value, ok := zero[Edge{"X", "Y"}]; !ok || value != (EdgeScore{}) {
		t.Fatal("valid zero contribution was discarded")
	}
}

func TestKGBoostAndFusionPreserveInputOwnership(t *testing.T) {
	entity := &KGEntity{Sim: 0.5, PageRank: 1, NhopEnts: []NhopEntity{{Path: []string{"A", "B"}, Weights: []float64{0.5}}}}
	entities := map[string]*KGEntity{"A": entity, "nil": nil}
	boosted := DoubleHitBoost(entities, map[string]struct{}{"A": {}})
	if boosted["A"].Sim != 1 || entity.Sim != 0.5 || len(boosted) != 1 {
		t.Fatalf("invalid boost or source mutation: %#v %#v", boosted, entity)
	}
	boosted["A"].NhopEnts[0].Path[0] = "changed"
	boosted["A"].NhopEnts[0].Weights[0] = 42
	if entity.NhopEnts[0].Path[0] != "A" || entity.NhopEnts[0].Weights[0] != 0.5 {
		t.Fatal("neighbor data aliases source")
	}
	relation := &KGRelation{Sim: 0.5, PageRank: 0.8, Description: "kept"}
	relations := map[Edge]*KGRelation{{"A", "B"}: relation}
	paths := map[Edge]EdgeScore{{"A", "B"}: {0.3, 0.7}, {"B", "C"}: {0.4, 0.6}}
	fused := FuseRelationScores(relations, map[string]struct{}{"A": {}, "B": {}}, paths)
	if math.Abs(fused[Edge{"A", "B"}].Sim-1.65) > 1e-12 || fused[Edge{"B", "C"}].Sim != 0.8 {
		t.Fatalf("unexpected fused scores: %#v", fused)
	}
	if len(relations) != 1 || len(paths) != 2 || relation.Sim != 0.5 || fused[Edge{"A", "B"}] == relation {
		t.Fatal("fusion changed or aliased caller data")
	}
	fused[Edge{"A", "B"}].Description = "changed"
	if relation.Description != "kept" {
		t.Fatal("relation pointer aliases source")
	}
}

func TestKGSortingLimitsTiesAndInvalidScores(t *testing.T) {
	entities := map[string]*KGEntity{
		"B": {Sim: 1, PageRank: 1}, "A": {Sim: 2, PageRank: 0.5},
		"zero": {}, "nil": nil, "nan": {Sim: math.NaN(), PageRank: 1},
		"inf": {Sim: 1, PageRank: math.Inf(1)}, "overflow": {Sim: math.MaxFloat64, PageRank: 2},
	}
	for i := 0; i < 100; i++ {
		got := SortAndTrimEntities(entities, 3)
		if len(got) != 3 || got[0].Entity != "A" || got[1].Entity != "B" || got[2].Entity != "zero" || got[2].Score != 0 {
			t.Fatalf("unstable or invalid ranking: %#v", got)
		}
	}
	for _, limit := range []int{-1, 0} {
		if len(SortAndTrimEntities(entities, limit)) != 0 || len(SortAndTrimRelations(map[Edge]*KGRelation{{"A", "B"}: {Sim: 1, PageRank: 1}}, limit)) != 0 {
			t.Fatal("nonpositive limit broadened selection")
		}
	}
	relations := map[Edge]*KGRelation{{"A", "C"}: {Sim: 1, PageRank: 1}, {"A", "B"}: {Sim: 1, PageRank: 1}, {"B", "A"}: nil}
	got := SortAndTrimRelations(relations, 2)
	if len(got) != 2 || got[0].To != "B" || got[1].To != "C" {
		t.Fatalf("relation tie order = %#v", got)
	}
	if len(DoubleHitBoost(map[string]*KGEntity{"overflow": {Sim: math.MaxFloat64}}, map[string]struct{}{"overflow": {}})) != 0 {
		t.Fatal("overflowing boost was retained")
	}
	if len(FuseRelationScores(map[Edge]*KGRelation{{"A", "B"}: {Sim: math.MaxFloat64}}, map[string]struct{}{"A": {}}, nil)) != 0 {
		t.Fatal("overflowing fusion was retained")
	}
}

func TestKGCSVDescriptionEscapingAndBudget(t *testing.T) {
	entities := []ScoredEntity{{Entity: "A,\"B", Score: 0, Description: `{"description":"line one, \"quoted\"\nline two 雪"}`}}
	output, remaining := FormatEntitiesToCSV(entities, 1000)
	rows, err := csv.NewReader(strings.NewReader(strings.TrimPrefix(output, "---- Entities ----\n"))).ReadAll()
	if err != nil || len(rows) != 2 || rows[1][0] != "A,\"B" || rows[1][1] != "0.00" || rows[1][2] != "line one, \"quoted\"\nline two 雪" {
		t.Fatalf("CSV round trip = %#v, %v; output %q", rows, err, output)
	}
	if remaining != 1000-NumTokensFromString(output) {
		t.Fatal("header or row omitted from budget")
	}
	budget := NumTokensFromString(output)
	if exact, left := FormatEntitiesToCSV(entities, budget); exact != output || left != 0 {
		t.Fatal("exact budget should retain complete output")
	}
	if short, left := FormatEntitiesToCSV(entities, budget-1); short != "" || left != budget-1 {
		t.Fatal("insufficient budget emitted a partial row or header")
	}
	for _, limit := range []int{-1, 0, 1, 10, budget, 1000} {
		content := BuildKGContent(entities, []ScoredRelation{{From: "A", To: "B", Score: 1, Description: "rel"}}, limit)
		if NumTokensFromString(content) > max(0, limit) {
			t.Fatalf("combined output exceeds budget %d: %q", limit, content)
		}
	}
	if NumTokensFromString("a") != 1 || NumTokensFromString("") != 0 || extractDescription(`{"other":"x"}`) != "" || extractDescription("plain description") != "plain description" {
		t.Fatal("invalid short-token or description semantics")
	}
}
