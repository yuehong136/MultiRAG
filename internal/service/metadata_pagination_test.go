package service

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"

	"multirag/internal/common"
	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity/models"
)

func TestMetadataDirectoryPaginationPreservesLargeNegativeSelection(t *testing.T) {
	const total = 12000
	ctx := t.Context()
	calls := 0
	store := &referenceMetadataEngine{search: func(got context.Context, request *types.SearchRequest) (*types.SearchResult, error) {
		calls++
		if got != ctx || !reflect.DeepEqual(request.IndexNames, []string{BuildMetadataIndexName("owner")}) || !reflect.DeepEqual(request.KbIDs, []string{"kb"}) || !request.IncludeUnavailable || request.Limit > 1000 || request.Limit < 1 || !reflect.DeepEqual(request.OrderBy, new(types.OrderByExpr).Asc("kb_id").Asc("id")) {
			t.Fatal("metadata pagination lost scope, context, policy or stable order", request)
		}
		rows := []map[string]interface{}{}
		for i := request.Offset; i < min(request.Offset+request.Limit, total); i++ {
			value := "keep"
			if i < 30 {
				value = "0"
			}
			rows = append(rows, map[string]interface{}{"id": fmt.Sprintf("doc-%05d", i), "kb_id": "kb", "meta_fields": map[string]interface{}{"canon": value, "large": json.Number("9007199254740993"), "zero": 0, "false": false}})
		}
		return &types.SearchResult{Total: total, Chunks: rows}, nil
	}}
	svc := &MetadataService{docEngine: store}
	rows, err := svc.searchMetadataDirectory(ctx, "owner", []string{"kb"}, 10000)
	if err != nil || len(rows) != total || calls != 12 {
		t.Fatal("metadata directory did not read every document", len(rows), calls, err)
	}
	directory, err := flattenMetadataRows(rows)
	if err != nil {
		t.Fatal(err)
	}
	ids, empty, err := ApplyMetaDataFilterChecked(ctx, manualMetadataConfig([]MetaFilterCondition{{Key: "canon", Op: "not in", Value: []interface{}{"0"}}}, "and"), directory, "q", nil, nil)
	if err != nil || empty || len(ids) != total-30 || ids[len(ids)-1] != "doc-11999" {
		t.Fatal("negative predicate lost documents after the old cap", len(ids), empty, err)
	}
	for key, value := range map[string]interface{}{"large": json.Number("9007199254740993"), "zero": 0, "false": false} {
		ids = ApplyMetaFilter(directory, []MetaFilterCondition{{Key: key, Op: "=", Value: value}}, "and")
		if len(ids) != total {
			t.Fatal("exact scalar was changed during pagination", key, len(ids))
		}
	}
}

func TestMetadataDirectoryPaginationRejectsIncompleteOrUnstableResults(t *testing.T) {
	row := func(id, kb string) map[string]interface{} {
		return map[string]interface{}{"id": id, "kb_id": kb, "meta_fields": map[string]interface{}{"number": 0}}
	}
	for name, pages := range map[string][]*types.SearchResult{
		"nil":                 {nil},
		"negative total":      {{Total: -1}},
		"underreported total": {{Total: 0, Chunks: []map[string]interface{}{row("a", "kb")}}},
		"overfull page":       {{Total: 2, Chunks: []map[string]interface{}{row("a", "kb"), row("b", "kb")}}},
		"no progress":         {{Total: 2, Chunks: []map[string]interface{}{row("a", "kb")}}, {Total: 2}},
		"changed total":       {{Total: 2, Chunks: []map[string]interface{}{row("a", "kb")}}, {Total: 1}},
		"duplicate identity":  {{Total: 2, Chunks: []map[string]interface{}{row("a", "kb")}}, {Total: 2, Chunks: []map[string]interface{}{row("a", "kb")}}},
		"foreign dataset":     {{Total: 1, Chunks: []map[string]interface{}{row("a", "foreign")}}},
		"missing identity":    {{Total: 1, Chunks: []map[string]interface{}{row("", "kb")}}},
	} {
		t.Run(name, func(t *testing.T) {
			calls := 0
			svc := &MetadataService{docEngine: &referenceMetadataEngine{search: func(context.Context, *types.SearchRequest) (*types.SearchResult, error) {
				if calls >= len(pages) {
					t.Fatal("invalid metadata repeated indefinitely")
				}
				page := pages[calls]
				calls++
				return page, nil
			}}}
			if rows, err := svc.searchMetadataDirectory(t.Context(), "owner", []string{"kb"}, 1); err == nil || rows != nil {
				t.Fatal("invalid directory yielded partial success", rows, err)
			}
		})
	}
}

func TestMetadataDirectoryCancellationAndFailureNeverReturnPartialRows(t *testing.T) {
	failure := errors.New("controlled metadata failure")
	for _, cancelDuring := range []bool{false, true} {
		ctx, cancel := context.WithCancel(t.Context())
		calls := 0
		svc := &MetadataService{docEngine: &referenceMetadataEngine{search: func(got context.Context, request *types.SearchRequest) (*types.SearchResult, error) {
			calls++
			if calls == 1 {
				return &types.SearchResult{Total: 2, Chunks: []map[string]interface{}{{"id": "a", "kb_id": "kb"}}}, nil
			}
			if cancelDuring {
				cancel()
				return &types.SearchResult{Total: 2, Chunks: []map[string]interface{}{{"id": "b", "kb_id": "kb"}}}, nil
			}
			return nil, failure
		}}}
		rows, err := svc.searchMetadataDirectory(ctx, "owner", []string{"kb"}, 1)
		want := failure
		if cancelDuring {
			want = context.Canceled
		}
		if !errors.Is(err, want) || rows != nil || calls != 2 {
			t.Fatal("metadata failure became a partial directory", rows, calls, err)
		}
		cancel()
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if rows, err := (&MetadataService{}).searchMetadataDirectory(ctx, "owner", []string{"kb"}, 1); !errors.Is(err, context.Canceled) || rows != nil {
		t.Fatal("pre-cancelled selection required dependencies", rows, err)
	}
}

func TestMetadataDirectoryRejectsCorruptEncodedFields(t *testing.T) {
	raw := []byte(`{"large":9007199254740993}`)
	packed := make([]byte, 4)
	binary.LittleEndian.PutUint32(packed, uint32(len(raw)))
	packed = append(packed, raw...)
	for _, value := range []interface{}{"{broken", `{"x":1} trailing`, []byte("{broken"), append(append([]byte(nil), packed...), 0), 5} {
		if rows, err := flattenMetadataRows([]map[string]interface{}{{"id": "doc", "meta_fields": value}}); err == nil || rows != nil {
			t.Fatal("corrupt metadata became a successful empty directory", value, rows, err)
		}
	}
}

type metadataSnapshotFixture struct {
	engine.DocEngine
	scan func(context.Context, *types.SearchRequest, func([]map[string]interface{}) error) error
}

func (f *metadataSnapshotFixture) ScanMetadata(ctx context.Context, request *types.SearchRequest, visit func([]map[string]interface{}) error) error {
	return f.scan(ctx, request, visit)
}

func TestMetadataDirectoryUsesSnapshotCapabilityAndRejectsVisitorFailures(t *testing.T) {
	for _, mode := range []string{"valid", "foreign", "duplicate", "scanner error", "cancel"} {
		t.Run(mode, func(t *testing.T) {
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			fixture := &metadataSnapshotFixture{scan: func(got context.Context, request *types.SearchRequest, visit func([]map[string]interface{}) error) error {
				if got != ctx || request.Offset != 0 || request.Limit != 1000 || !reflect.DeepEqual(request.IndexNames, []string{BuildMetadataIndexName("owner")}) || !reflect.DeepEqual(request.KbIDs, []string{"kb"}) || !request.IncludeUnavailable {
					t.Fatal("snapshot request lost scope", request)
				}
				if err := visit([]map[string]interface{}{{"id": "a", "kb_id": "kb"}}); err != nil {
					return err
				}
				switch mode {
				case "foreign":
					return visit([]map[string]interface{}{{"id": "b", "kb_id": "other"}})
				case "duplicate":
					return visit([]map[string]interface{}{{"id": "a", "kb_id": "kb"}})
				case "scanner error":
					return errors.New("controlled scan failure")
				case "cancel":
					cancel()
					return nil
				}
				return visit([]map[string]interface{}{{"id": "b", "kb_id": "kb"}})
			}}
			rows, err := (&MetadataService{docEngine: fixture}).searchMetadataDirectory(ctx, "owner", []string{"kb"}, 10000)
			if mode == "valid" {
				if err != nil || len(rows) != 2 {
					t.Fatal("valid snapshot failed", rows, err)
				}
			} else if err == nil || rows != nil {
				t.Fatal("snapshot yielded partial success", rows, err)
			}
		})
	}
}

func TestMetadataGeneratedFilterPreservesExactNumericOperandAndStablePrompt(t *testing.T) {
	answer := `{"conditions":[{"key":"large","op":"=","value":9007199254740993}],"logic":"and"}`
	name := "controlled"
	calls := 0
	driver := &searchbotTestDriver{chat: func(_ string, _ *models.APIConfig, messages []models.Message, _ *models.ChatConfig) (*models.ChatResponse, error) {
		calls++
		if len(messages) == 0 || !strings.Contains(messages[0].Content.(string), `"large":["0","9007199254740992","9007199254740993"]`) {
			t.Fatal("metadata prompt values were not deterministically ordered", messages)
		}
		return &models.ChatResponse{Answer: &answer}, nil
	}}
	model := models.NewChatModel(driver, &name, nil)
	directory := common.MetaData{"large": {"9007199254740993": {"exact"}, "9007199254740992": {"adjacent"}, "0": {"zero"}}}
	ids, empty, err := ApplyMetaDataFilterChecked(t.Context(), map[string]interface{}{"method": "auto"}, directory, "q", model, nil)
	if err != nil || empty || calls != 1 || !reflect.DeepEqual(ids, []string{"exact"}) {
		t.Fatal("generated numeric operand was rounded", ids, empty, calls, err)
	}
}
