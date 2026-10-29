package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"reflect"
	"testing"

	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/service/nlp"
)

type checkedTagEngine struct {
	engine.DocEngine
	owners        map[string]string
	all, relevant map[string][]map[string]interface{}
	requests      []types.SearchRequest
	search        func(context.Context, *types.SearchRequest) (*types.SearchResult, error)
}

func (e *checkedTagEngine) Search(ctx context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	copy := *req
	e.requests = append(e.requests, copy)
	if e.search != nil {
		return e.search(ctx, req)
	}
	if len(req.KbIDs) != 1 || !reflect.DeepEqual(req.IndexNames, []string{"multirag_" + e.owners[req.KbIDs[0]]}) || req.IncludeUnavailable || req.Limit != 1000 {
		return nil, errors.New("unscoped tag request")
	}
	rows := e.all[req.KbIDs[0]]
	if len(req.MatchExprs) > 0 {
		rows = e.relevant[req.KbIDs[0]]
	}
	begin, end := req.Offset, min(req.Offset+req.Limit, len(rows))
	if begin > len(rows) {
		begin = len(rows)
	}
	return &types.SearchResult{Chunks: rows[begin:end], Total: int64(len(rows))}, nil
}
func checkedTagRow(id, kb, tag string) map[string]interface{} {
	return map[string]interface{}{"id": id, "kb_id": kb, "tag_kwd": tag}
}
func checkedTagSource(ids interface{}, top interface{}) *Knowledgebase {
	config := map[string]interface{}{"tag_kb_ids": ids}
	if top != nil {
		config["topn_tags"] = top
	}
	return &Knowledgebase{ID: "source", TenantID: "source-owner", ParserConfig: config}
}
func checkedTagResolver(owners map[string]string, calls *[]string) tagKnowledgebaseReader {
	return func(ctx context.Context, id, user string) (*Knowledgebase, error) {
		*calls = append(*calls, id)
		if user != "actor" {
			return nil, errors.New("wrong actor")
		}
		if owners[id] == "" {
			return nil, errors.New("denied")
		}
		return &Knowledgebase{ID: id, TenantID: owners[id]}, ctx.Err()
	}
}

func TestCheckedTagLabelsAuthorizeWholeSelectionAndPairOwners(t *testing.T) {
	if err := nlp.InitQueryBuilder(""); err != nil {
		t.Fatal(err)
	}
	e := &checkedTagEngine{owners: map[string]string{"first": "tenant-a", "second": "tenant-b"}, all: map[string][]map[string]interface{}{}, relevant: map[string][]map[string]interface{}{}}
	for _, kb := range []string{"first", "second"} {
		e.all[kb] = []map[string]interface{}{checkedTagRow("a", kb, "alpha###beta"), checkedTagRow("b", kb, "alpha")}
		e.relevant[kb] = e.all[kb][:1]
	}
	svc := &MetadataService{docEngine: e}
	var reads []string
	got, err := svc.labelQuestionContext(t.Context(), "actor", "question", []*Knowledgebase{checkedTagSource([]string{"first"}, nil), checkedTagSource([]interface{}{"second", "first"}, 2)}, checkedTagResolver(e.owners, &reads))
	if err != nil || len(got) != 2 || got["alpha"] < 1 || got["beta"] < 1 || !reflect.DeepEqual(reads, []string{"first", "second"}) || len(e.requests) != 4 {
		t.Fatal(got, reads, e.requests, err)
	}
	e.requests = nil
	reads = nil
	got, err = svc.labelQuestionContext(t.Context(), "actor", "question", []*Knowledgebase{checkedTagSource([]string{"first", "denied"}, nil)}, checkedTagResolver(e.owners, &reads))
	if err == nil || got != nil || len(e.requests) != 0 || !reflect.DeepEqual(reads, []string{"first", "denied"}) {
		t.Fatal("partial authorization reached engine", got, reads, err)
	}
}

func TestCheckedTagLabelsReadCompleteDirectoryBeyondTenThousand(t *testing.T) {
	if err := nlp.InitQueryBuilder(""); err != nil {
		t.Fatal(err)
	}
	e := &checkedTagEngine{owners: map[string]string{"tags": "owner"}, all: map[string][]map[string]interface{}{}, relevant: map[string][]map[string]interface{}{}}
	for i := 0; i < 10001; i++ {
		tag := "common"
		if i == 10000 {
			tag = "tail"
		}
		e.all["tags"] = append(e.all["tags"], checkedTagRow(fmt.Sprintf("%05d", i), "tags", tag))
	}
	e.relevant["tags"] = e.all["tags"][10000:]
	svc := &MetadataService{docEngine: e}
	var reads []string
	got, err := svc.labelQuestionContext(t.Context(), "actor", "question", []*Knowledgebase{checkedTagSource([]string{"tags"}, 1)}, checkedTagResolver(e.owners, &reads))
	if err != nil || len(got) != 1 || got["tail"] < 1 || len(e.requests) != 12 || e.requests[10].Offset != 10000 || len(e.requests[11].MatchExprs) != 1 {
		t.Fatal("directory truncated or query not used", got, len(e.requests), err)
	}
}

func TestCheckedTagValidationAndZeroHaveNoIndexSideEffects(t *testing.T) {
	e := &checkedTagEngine{owners: map[string]string{"tags": "owner"}}
	svc := &MetadataService{docEngine: e}
	var reads []string
	for _, kb := range []*Knowledgebase{
		checkedTagSource("tags", nil), checkedTagSource([]interface{}{"tags", 42}, nil), checkedTagSource([]string{"tags", ""}, nil),
		checkedTagSource([]string{"tags"}, math.NaN()), checkedTagSource([]string{"tags"}, math.Inf(1)), checkedTagSource([]string{"tags"}, -1), checkedTagSource([]string{"tags"}, 1.5), checkedTagSource([]string{"tags"}, json.Number("1e3")),
	} {
		if got, err := svc.labelQuestionContext(t.Context(), "actor", "q", []*Knowledgebase{kb}, checkedTagResolver(e.owners, &reads)); err == nil || got != nil || len(reads) != 0 || len(e.requests) != 0 {
			t.Fatal("invalid selector performed IO", got, reads, err)
		}
	}
	got, err := svc.labelQuestionContext(t.Context(), "actor", "q", []*Knowledgebase{checkedTagSource([]string{"tags"}, 0)}, checkedTagResolver(e.owners, &reads))
	if err != nil || got == nil || len(got) != 0 || !reflect.DeepEqual(reads, []string{"tags"}) || len(e.requests) != 0 {
		t.Fatal("legal zero broadened selection", got, reads, err)
	}
	reads = nil
	got, err = svc.labelQuestionContext(t.Context(), "actor", "q", []*Knowledgebase{checkedTagSource([]string{}, nil)}, checkedTagResolver(e.owners, &reads))
	if err != nil || got != nil || len(reads) != 0 || len(e.requests) != 0 {
		t.Fatal("empty selector broadened", got, reads, err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if _, err := svc.labelQuestionContext(ctx, "actor", "q", []*Knowledgebase{checkedTagSource([]string{"tags"}, nil)}, checkedTagResolver(e.owners, &reads)); !errors.Is(err, context.Canceled) || len(reads) != 0 {
		t.Fatal("cancellation reached DAO", err)
	}
}

func TestCheckedTagDirectoryRejectsScopePaginationAndStorageFailures(t *testing.T) {
	boom := errors.New("store unavailable")
	cases := []struct {
		name  string
		pages []*types.SearchResult
		err   error
	}{
		{"foreign", []*types.SearchResult{{Total: 1, Chunks: []map[string]interface{}{checkedTagRow("a", "foreign", "x")}}}, nil},
		{"missing-id", []*types.SearchResult{{Total: 1, Chunks: []map[string]interface{}{{"kb_id": "tags", "tag_kwd": "x"}}}}, nil},
		{"bad-tag", []*types.SearchResult{{Total: 1, Chunks: []map[string]interface{}{{"id": "a", "kb_id": "tags", "tag_kwd": []interface{}{true}}}}}, nil},
		{"duplicate", []*types.SearchResult{{Total: 2, Chunks: []map[string]interface{}{checkedTagRow("a", "tags", "x")}}, {Total: 2, Chunks: []map[string]interface{}{checkedTagRow("a", "tags", "x")}}}, nil},
		{"changed-total", []*types.SearchResult{{Total: 2, Chunks: []map[string]interface{}{checkedTagRow("a", "tags", "x")}}, {Total: 3, Chunks: []map[string]interface{}{checkedTagRow("b", "tags", "x")}}}, nil},
		{"no-progress", []*types.SearchResult{{Total: 1}}, nil},
		{"exceeded-total", []*types.SearchResult{{Total: 0, Chunks: []map[string]interface{}{checkedTagRow("a", "tags", "x")}}}, nil},
		{"missing-result", []*types.SearchResult{nil}, nil},
		{"negative-total", []*types.SearchResult{{Total: -1}}, nil},
		{"storage", nil, boom},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			call := 0
			e := &checkedTagEngine{search: func(context.Context, *types.SearchRequest) (*types.SearchResult, error) {
				if tc.err != nil {
					return nil, tc.err
				}
				if call >= len(tc.pages) {
					t.Fatal("extra read")
				}
				page := tc.pages[call]
				call++
				return page, nil
			}}
			got, err := (&MetadataService{docEngine: e}).readTagCounts(t.Context(), &Knowledgebase{ID: "tags", TenantID: "owner"}, nil)
			if err == nil || got != nil {
				t.Fatal("invalid directory accepted", got, err)
			}
			if tc.err != nil && !errors.Is(err, boom) {
				t.Fatal("storage error lost", err)
			}
		})
	}
}

func TestCheckedTagScoringUsesStableTiesAndSmoothing(t *testing.T) {
	for i := 0; i < 20; i++ {
		got, err := scoreQuestionTags(map[string]int64{"z": 1, "a.b": 1}, map[string]int64{"z": 1, "a.b": 1}, 1)
		if err != nil || !reflect.DeepEqual(got, map[string]float64{"a_b": 1}) {
			t.Fatal("tie changed", got, err)
		}
	}
	if got, err := scoreQuestionTags(map[string]int64{"a": math.MaxInt64, "b": 1}, map[string]int64{"a": 1}, 1); err == nil || got != nil {
		t.Fatal("overflow accepted", got, err)
	}
}
