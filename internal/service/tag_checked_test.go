package service

import (
	"context"
	"errors"
	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"reflect"
	"testing"
)

type canonicalTagEngine struct {
	engine.DocEngine
	request *types.SearchRequest
	failure error
}

func (f *canonicalTagEngine) Search(_ context.Context, request *types.SearchRequest) (*types.SearchResult, error) {
	f.request = request
	if f.failure != nil {
		return nil, f.failure
	}
	return &types.SearchResult{Chunks: []map[string]interface{}{{"tag_kwd": "alpha"}, {"tag_kwd": "beta"}}}, nil
}
func (f *canonicalTagEngine) GetAggregation([]map[string]interface{}, string) []map[string]interface{} {
	return []map[string]interface{}{{"key": "alpha", "count": 2}, {"key": "beta", "count": 1}}
}
func TestCanonicalTagDistributionScopeAndSmoothing(t *testing.T) {
	index := &canonicalTagEngine{}
	s := &MetadataService{docEngine: index}
	values, err := s.GetAllTagsInPortion("owner", []string{"first", "second"})
	if err != nil || values["alpha"] != 3.0/1003 || values["beta"] != 2.0/1003 {
		t.Fatal("smoothing", values, err)
	}
	if !reflect.DeepEqual(index.request.IndexNames, []string{"multirag_owner"}) || !reflect.DeepEqual(index.request.KbIDs, []string{"first", "second"}) || index.request.Limit != 10000 {
		t.Fatal("canonical tag query scope", index.request)
	}
	failure := errors.New("controlled tag failure")
	index.failure = failure
	if result, err := s.GetAllTagsInPortion("owner", []string{"first"}); result != nil || !errors.Is(err, failure) {
		t.Fatal(result, err)
	}
	if result, err := (&MetadataService{}).GetAllTagsInPortion("owner", nil); err != nil || len(result) != 0 {
		t.Fatal(result, err)
	}
}
func TestCanonicalTagZeroHasNoIndexSideEffects(t *testing.T) {
	values, err := (&MetadataService{}).TagQuery("q", []string{"owner"}, []string{"kb"}, map[string]float64{"tag": 1}, 0)
	if err != nil || values == nil || len(values) != 0 {
		t.Fatal(values, err)
	}
}
