package infinity

import (
	"context"
	"errors"
	"math"
	"reflect"
	"testing"

	sdk "github.com/infiniflow/infinity-go-sdk"

	"multirag/internal/engine/types"
)

func TestKGInfinityExplicitFusionDenseOptionsKeepScalarScope(t *testing.T) {
	fields := []string{"entity_kwd^10", "content^2"}
	dense := &types.MatchDenseExpr{ExtraOptions: map[string]any{"similarity": 0.3}}
	for _, question := range []string{"semantic-only candidate", "What's the entity?", `What's the "entity"?`} {
		got, err := infinityDenseOptions("available_int=1 AND kb_id = 'owned-kb'", fields, question, dense, true)
		want := map[string]string{"threshold": "0.3", "filter": "available_int=1 AND kb_id = 'owned-kb'"}
		if err != nil || !reflect.DeepEqual(got, want) {
			t.Fatalf("explicit fusion lost scalar scope or required a lexical hit: %#v, %v", got, err)
		}
	}
	if !reflect.DeepEqual(fields, []string{"entity_kwd^10", "content^2"}) || dense.ExtraOptions["similarity"] != 0.3 {
		t.Fatal("caller fields or dense options mutated")
	}
}

func TestKGInfinityLegacyDenseOptionsQuoteQuestionExactly(t *testing.T) {
	fields := []string{"entity_kwd^10", "content^2"}
	for _, test := range []struct {
		question, filter string
	}{
		{"plain entity", "(available_int=1) AND filter_fulltext('entity_kwd^10,content^2', 'plain entity')"},
		{"What's the entity?", `(available_int=1) AND filter_fulltext('entity_kwd^10,content^2', "What's the entity?")`},
		{`the "entity"`, `(available_int=1) AND filter_fulltext('entity_kwd^10,content^2', 'the "entity"')`},
	} {
		got, err := infinityDenseOptions("", fields, test.question, nil, false)
		want := map[string]string{"threshold": "0.0", "filter": test.filter}
		if err != nil || !reflect.DeepEqual(got, want) {
			t.Fatalf("legacy dense filter changed or broke question quoting: %#v, %v", got, err)
		}
		parsed, err := sdk.ParseFilter(got["filter"])
		if err != nil || parsed == nil || parsed.Type == nil || parsed.Type.FunctionExpr == nil || len(parsed.Type.FunctionExpr.Arguments) != 2 {
			t.Fatalf("pinned SDK rejected the generated legacy filter: %#v, %v", parsed, err)
		}
		fulltext := parsed.Type.FunctionExpr.Arguments[1]
		if fulltext == nil || fulltext.Type == nil || fulltext.Type.FunctionExpr == nil || fulltext.Type.FunctionExpr.FunctionName != "filter_fulltext" || len(fulltext.Type.FunctionExpr.Arguments) != 2 {
			t.Fatal("pinned SDK lost the lexical prefilter", fulltext)
		}
		arguments := fulltext.Type.FunctionExpr.Arguments
		for i, expected := range []string{"entity_kwd^10,content^2", test.question} {
			if arguments[i] == nil || arguments[i].Type == nil || arguments[i].Type.ConstantExpr == nil || arguments[i].Type.ConstantExpr.StrValue == nil || *arguments[i].Type.ConstantExpr.StrValue != expected {
				t.Fatalf("pinned SDK changed fulltext argument %d: %#v", i, arguments[i])
			}
		}
	}
	if got, err := infinityDenseOptions("kb_id = 'owned-kb'", fields, `What's the "entity"?`, nil, false); err == nil || got != nil {
		t.Fatal("unrepresentable legacy fulltext literal broadened the filter", got, err)
	}
	got, err := infinityDenseOptions("kb_id = 'owned-kb'", fields, "", nil, false)
	if err != nil || !reflect.DeepEqual(got, map[string]string{"threshold": "0.0", "filter": "kb_id = 'owned-kb'"}) {
		t.Fatal("dense-only scope changed", got, err)
	}
}

func TestKGInfinityDenseSimilarityFiniteAndExplicitZero(t *testing.T) {
	for _, similarity := range []float64{0, 0.3} {
		dense := &types.MatchDenseExpr{ExtraOptions: map[string]any{"similarity": similarity}}
		got, err := infinityDenseOptions("kb_id = 'owned-kb'", nil, "", dense, true)
		threshold := "0.0"
		if similarity == 0.3 {
			threshold = "0.3"
		}
		if err != nil || !reflect.DeepEqual(got, map[string]string{"threshold": threshold, "filter": "kb_id = 'owned-kb'"}) {
			t.Fatal("similarity option lost", got, err)
		}
	}
	for _, similarity := range []any{math.NaN(), math.Inf(1), math.Inf(-1), nil, "0.3", float32(0.3), 0} {
		dense := &types.MatchDenseExpr{ExtraOptions: map[string]any{"similarity": similarity}}
		if got, err := infinityDenseOptions("kb_id = 'owned-kb'", nil, "", dense, true); err == nil || got != nil {
			t.Fatal("invalid similarity produced SDK options", got, err)
		}
	}
}

func TestKGInfinityInvalidSimilarityFailsBeforeSDKAccess(t *testing.T) {
	store := &infinityEngine{}
	request := &types.SearchRequest{
		IndexNames: []string{"multirag_owned"},
		KbIDs:      []string{"owned-kb"},
		MatchExprs: []any{&types.MatchDenseExpr{
			EmbeddingData: []float64{1, 0},
			ExtraOptions:  map[string]any{"similarity": math.NaN()},
		}},
	}
	// A client access would panic: invalid options must be rejected before any
	// database or table lookup, without requiring a running Infinity service.
	if result, err := store.Search(t.Context(), request); err == nil || result != nil {
		t.Fatal("invalid similarity reached the SDK client", result, err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	request.MatchExprs = nil
	if result, err := store.Search(ctx, request); !errors.Is(err, context.Canceled) || result != nil {
		t.Fatal("cancelled search reached the SDK client", result, err)
	}
}
