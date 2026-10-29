package service

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"reflect"
	"sort"
	"testing"

	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
)

type documentMetadataScopeCall struct {
	ctx     context.Context
	request types.SearchRequest
}

type documentMetadataScopeEngine struct {
	engine.DocEngine
	rows     []map[string]interface{}
	calls    []documentMetadataScopeCall
	err      error
	after    func()
	total    *int64
	nilReply bool
}

func (e *documentMetadataScopeEngine) Search(ctx context.Context, request *types.SearchRequest) (*types.SearchResult, error) {
	copy := *request
	copy.IndexNames = append([]string(nil), request.IndexNames...)
	copy.KbIDs = append([]string(nil), request.KbIDs...)
	copy.SelectFields = append([]string(nil), request.SelectFields...)
	copy.Filter = make(map[string]interface{}, len(request.Filter))
	for key, value := range request.Filter {
		if ids, ok := value.([]string); ok {
			value = append([]string(nil), ids...)
		}
		copy.Filter[key] = value
	}
	e.calls = append(e.calls, documentMetadataScopeCall{ctx: ctx, request: copy})
	if e.after != nil {
		e.after()
	}
	if e.err != nil || e.nilReply {
		return nil, e.err
	}
	total := int64(len(e.rows))
	if e.total != nil {
		total = *e.total
	}
	rows := e.rows
	if _, explicit := request.Filter["id"]; !explicit {
		rows = append([]map[string]interface{}(nil), rows...)
		sort.SliceStable(rows, func(i, j int) bool {
			left, right := fmt.Sprint(rows[i]["kb_id"]), fmt.Sprint(rows[j]["kb_id"])
			if left != right {
				return left < right
			}
			return fmt.Sprint(rows[i]["id"]) < fmt.Sprint(rows[j]["id"])
		})
		start := min(max(request.Offset, 0), len(rows))
		end := min(start+max(request.Limit, 0), len(rows))
		rows = rows[start:end]
	}
	return &types.SearchResult{Chunks: rows, Total: total}, nil
}

type documentMetadataScopeContextKey struct{}

const documentMetadataScopeJSON = `{"zero":0,"big":9007199254740993,"enabled":false,"empty":""}`

func documentMetadataScopeRows(ids map[string]string, value interface{}) []map[string]interface{} {
	return []map[string]interface{}{{"id": ids["doc"], "kb_id": ids["kb"], "meta_fields": value}}
}

func documentMetadataScopeReader(f *documentFilesFixture, index *documentMetadataScopeEngine) *DocumentService {
	return &DocumentService{db: f.db, documentDAO: dao.NewDocumentDAO(), kbDAO: dao.NewKnowledgebaseDAO(), docEngine: index,
		metadataSvc: &MetadataService{kbDAO: dao.NewKnowledgebaseDAO(), docEngine: index}}
}

func documentMetadataAssertIndexScope(t *testing.T, index *documentMetadataScopeEngine, ctx context.Context, ids map[string]string, explicit bool) {
	t.Helper()
	if len(index.calls) != 1 {
		t.Fatal("authorized metadata read did not make exactly one index request")
	}
	call := index.calls[0]
	if call.ctx != ctx || !reflect.DeepEqual(call.request.IndexNames, []string{BuildMetadataIndexName(ids["owner"])}) ||
		!reflect.DeepEqual(call.request.KbIDs, []string{ids["kb"]}) ||
		!reflect.DeepEqual(call.request.SelectFields, []string{"id", "kb_id", "meta_fields"}) || !call.request.IncludeUnavailable || call.request.Offset != 0 {
		t.Fatal("metadata index request lost its request context or exact SQL-authorized scope")
	}
	if explicit {
		if !reflect.DeepEqual(call.request.Filter["id"], []string{ids["doc"]}) || call.request.Limit != 1 {
			t.Fatal("explicit metadata selection lost the exact active document filter")
		}
	} else if _, restricted := call.request.Filter["id"]; restricted || call.request.Limit != 1000 || call.request.OrderBy == nil || !reflect.DeepEqual(call.request.OrderBy.Fields, []types.OrderByField{{Field: "kb_id", Type: types.SortAsc}, {Field: "id", Type: types.SortAsc}}) {
		t.Fatal("all-document metadata directory lost stable complete pagination")
	}
}

func documentMetadataAssertScalars(t *testing.T, values map[string]interface{}) {
	t.Helper()
	if values["zero"] != json.Number("0") || values["big"] != json.Number("9007199254740993") || values["enabled"] != false || values["empty"] != "" {
		t.Fatal("document metadata lost JSON number precision or a valid falsy scalar")
	}
}

func documentMetadataAssertSummary(t *testing.T, values map[string]interface{}) {
	t.Helper()
	raw, err := json.Marshal(values)
	if err != nil {
		t.Fatal("metadata summary was not JSON serializable")
	}
	var summary map[string]struct {
		Type   string
		Values [][2]json.RawMessage
	}
	if json.Unmarshal(raw, &summary) != nil {
		t.Fatal("metadata summary contract changed")
	}
	for field, expected := range map[string]string{"zero": "0", "big": "9007199254740993", "enabled": "false", "empty": ""} {
		entry, ok := summary[field]
		var value string
		var count int
		if !ok || len(entry.Values) != 1 || json.Unmarshal(entry.Values[0][0], &value) != nil || value != expected || json.Unmarshal(entry.Values[0][1], &count) != nil || count != 1 {
			t.Fatal("metadata summary dropped or rounded a valid scalar")
		}
		if (field == "zero" || field == "big") && entry.Type != "number" {
			t.Fatal("metadata summary changed a JSON number into a string type")
		}
	}
}

func documentMetadataAssertDirectory(t *testing.T, values map[string]interface{}, docID string) {
	t.Helper()
	raw, err := json.Marshal(values)
	var directory map[string]map[string][]string
	if err != nil || json.Unmarshal(raw, &directory) != nil {
		t.Fatal("metadata directory contract changed")
	}
	for field, value := range map[string]string{"zero": "0", "big": "9007199254740993", "enabled": "false", "empty": ""} {
		if !reflect.DeepEqual(directory[field][value], []string{docID}) {
			t.Fatal("metadata directory dropped or rounded a valid scalar or changed document identity")
		}
	}
}

func documentMetadataScopeCalls(svc *DocumentService, ctx context.Context, userID, kbID, docID string) []func() (map[string]interface{}, error) {
	return []func() (map[string]interface{}, error){
		func() (map[string]interface{}, error) { return svc.GetMetadataSummary(ctx, kbID, userID, nil) },
		func() (map[string]interface{}, error) { return svc.GetDocumentMetadataByID(ctx, docID, userID) },
		func() (map[string]interface{}, error) { return svc.GetMetadataByKBs(ctx, userID, []string{kbID}) },
	}
}

func TestDocumentMetadataScopeSQL(t *testing.T) {
	f := newDocumentFilesFixture(t)
	ids := f.ids
	index := &documentMetadataScopeEngine{rows: documentMetadataScopeRows(ids, documentMetadataScopeJSON)}
	svc := documentMetadataScopeReader(f, index)
	ctx := context.WithValue(context.Background(), documentMetadataScopeContextKey{}, "request-scoped-metadata")
	f.journal.Event("reserve-controlled-document-metadata-engine", map[string]any{
		"metadata_sha256": fmt.Sprintf("%x", sha256.Sum256([]byte(documentMetadataScopeJSON))), "dataset": ids["kb"], "document": ids["doc"],
		"index": BuildMetadataIndexName(ids["owner"]), "index_backend": "controlled Search, real PostgreSQL authorization and independent SQL readback",
	})
	for _, role := range []string{"owner", "admin", "normal"} {
		t.Run(role+"-team-read", func(t *testing.T) {
			before := f.snapshot()
			index.calls = nil
			rows, total, err := svc.ListDocumentsByKBID(ctx, ids["kb"], ids[role], 1, 10)
			if err != nil || total != 1 || len(rows) != 1 || rows[0].ID != ids["doc"] || len(index.calls) != 0 {
				t.Fatal("authorized legacy listing lost active SQL scope or touched the index")
			}
			for position, call := range documentMetadataScopeCalls(svc, ctx, ids[role], ids["kb"], ids["doc"]) {
				index.calls = nil
				values, err := call()
				if err != nil {
					t.Fatal("authorized team metadata read failed")
				}
				documentMetadataAssertIndexScope(t, index, ctx, ids, position == 1)
				switch position {
				case 0:
					documentMetadataAssertSummary(t, values)
				case 1:
					documentMetadataAssertScalars(t, values)
				case 2:
					documentMetadataAssertDirectory(t, values, ids["doc"])
				}
			}
			if f.snapshot() != before {
				t.Fatal("authorized metadata reads changed SQL rows")
			}
		})
	}
	deny := func(t *testing.T, userID, kbID, docID string) {
		t.Helper()
		before := f.snapshot()
		index.calls = nil
		if rows, total, err := svc.ListDocumentsByKBID(ctx, kbID, userID, 1, 10); err == nil || rows != nil || total != 0 {
			t.Fatal("legacy document listing accepted an unauthorized or inactive SQL scope")
		}
		for _, call := range documentMetadataScopeCalls(svc, ctx, userID, kbID, docID) {
			if values, err := call(); err == nil || values != nil {
				t.Fatal("metadata reader accepted an unauthorized or inactive SQL scope")
			}
		}
		if len(index.calls) != 0 || f.snapshot() != before {
			t.Fatal("denied document scope reached the index or changed SQL rows")
		}
	}
	for _, test := range []struct{ name, user, kb, doc string }{
		{"outsider", "outsider", "kb", "doc"}, {"revoked", "revoked", "kb", "doc"}, {"invited", "invite", "kb", "doc"},
		{"disabled-actor", "disabled-user", "kb", "doc"}, {"foreign", "owner", "foreign-kb", "foreign-doc"},
		{"inactive-dataset", "normal", "inactive-kb", "inactive-kb-doc"}, {"inactive-tenant", "normal", "disabled-tenant-kb", "disabled-tenant-doc"},
	} {
		t.Run(test.name, func(t *testing.T) { deny(t, ids[test.user], ids[test.kb], ids[test.doc]) })
	}
	t.Run("private-dataset", func(t *testing.T) {
		if err := f.db.Model(&entity.Knowledgebase{}).Where("id=?", ids["kb"]).Update("permission", "me").Error; err != nil {
			t.Fatal("private dataset fixture setup failed")
		}
		t.Cleanup(func() {
			if err := f.db.Model(&entity.Knowledgebase{}).Where("id=?", ids["kb"]).Update("permission", "team").Error; err != nil {
				t.Error("private dataset fixture restoration failed")
			}
		})
		deny(t, ids["normal"], ids["kb"], ids["doc"])
		deny(t, ids["admin"], ids["kb"], ids["doc"])
		index.calls = nil
		if values, err := svc.GetDocumentMetadataByID(ctx, ids["doc"], ids["owner"]); err != nil || values == nil {
			t.Fatal("private dataset owner lost document metadata access")
		}
		documentMetadataAssertIndexScope(t, index, ctx, ids, true)
	})
	t.Run("complete-selection-authorized-before-index", func(t *testing.T) {
		before := f.snapshot()
		for _, selected := range [][]string{{ids["kb"], ids["foreign-kb"]}, {ids["foreign-kb"], ids["kb"]}} {
			index.calls = nil
			if values, err := svc.GetMetadataByKBs(ctx, ids["owner"], selected); err == nil || values != nil || len(index.calls) != 0 {
				t.Fatal("mixed dataset selection reached an index before complete authorization")
			}
		}
		for _, selected := range [][]string{{ids["doc"], ids["foreign-doc"]}, {ids["inactive-doc"]}, {ids["doc"], ids["inactive-doc"]}, {ids["doc"], "missing-document"}} {
			index.calls = nil
			if values, err := svc.GetMetadataSummary(ctx, ids["kb"], ids["normal"], selected); err == nil || values != nil || len(index.calls) != 0 {
				t.Fatal("foreign, missing or inactive selected document reached the index")
			}
		}
		index.calls = nil
		if values, err := svc.GetMetadataSummary(ctx, ids["kb"], ids["normal"], []string{}); err != nil || len(values) != 0 || len(index.calls) != 0 {
			t.Fatal("explicit empty document selection broadened to an index read")
		}
		if values, err := svc.GetMetadataSummary(ctx, ids["kb"], ids["outsider"], []string{}); err == nil || values != nil || len(index.calls) != 0 {
			t.Fatal("explicit empty document selection bypassed dataset authorization")
		}
		if values, err := svc.GetDocumentMetadataByID(ctx, ids["inactive-doc"], ids["owner"]); err == nil || values != nil || len(index.calls) != 0 {
			t.Fatal("inactive document metadata reached the index")
		}
		if f.snapshot() != before {
			t.Fatal("selection validation changed SQL rows")
		}
	})
	t.Run("cancellation", func(t *testing.T) {
		before := f.snapshot()
		canceled, cancel := context.WithCancel(ctx)
		cancel()
		index.calls = nil
		if rows, total, err := svc.ListDocumentsByKBID(canceled, ids["kb"], ids["normal"], 1, 10); !errors.Is(err, context.Canceled) || rows != nil || total != 0 {
			t.Fatal("canceled legacy listing did not preserve cancellation")
		}
		for _, call := range documentMetadataScopeCalls(svc, canceled, ids["normal"], ids["kb"], ids["doc"]) {
			if values, err := call(); !errors.Is(err, context.Canceled) || values != nil || len(index.calls) != 0 {
				t.Fatal("canceled metadata request reached the index")
			}
		}
		midSearch, stop := context.WithCancel(ctx)
		defer stop()
		index.after = stop
		defer func() { index.after = nil }()
		if values, err := svc.GetDocumentMetadataByID(midSearch, ids["doc"], ids["normal"]); !errors.Is(err, context.Canceled) || values != nil || len(index.calls) != 1 || index.calls[0].ctx != midSearch {
			t.Fatal("in-flight index cancellation was swallowed or lost its context")
		}
		if f.snapshot() != before {
			t.Fatal("canceled metadata read changed SQL rows")
		}
	})
	f.journal.Event("document-metadata-SQL-scope-independent-readback", map[string]any{
		"owner_admin_normal_team_reads": true, "private_members_denied": true, "foreign_inactive_revoked_denied": true,
		"mixed_selection_preflight": true, "explicit_empty_selection_no_index": true, "cancellation_preserved": true, "SQL_unchanged_by_reads": true,
	})
}

func documentMetadataLengthPrefix(values ...string) []byte {
	var result []byte
	for _, value := range values {
		prefix := make([]byte, 4)
		binary.LittleEndian.PutUint32(prefix, uint32(len(value)))
		result = append(result, prefix...)
		result = append(result, value...)
	}
	return result
}

func TestDocumentMetadataScopeIndexResults(t *testing.T) {
	f := newDocumentFilesFixture(t)
	ids := f.ids
	index := &documentMetadataScopeEngine{}
	svc := documentMetadataScopeReader(f, index)
	ctx := context.WithValue(context.Background(), documentMetadataScopeContextKey{}, "strict-metadata-results")
	f.journal.Event("reserve-controlled-strict-metadata-results", map[string]any{"dataset": ids["kb"], "document": ids["doc"], "index_backend": "controlled Search, real PostgreSQL authorization"})
	for _, test := range []struct {
		name  string
		value interface{}
	}{
		{"JSON-string", documentMetadataScopeJSON}, {"JSON-bytes", []byte(documentMetadataScopeJSON)},
		{"strict-length-prefix", documentMetadataLengthPrefix(`{"zero":0,"big":9007199254740993}`, `{"enabled":false,"empty":""}`)},
	} {
		t.Run(test.name, func(t *testing.T) {
			before := f.snapshot()
			index.rows = documentMetadataScopeRows(ids, test.value)
			for position, call := range documentMetadataScopeCalls(svc, ctx, ids["normal"], ids["kb"], ids["doc"]) {
				index.calls = nil
				values, err := call()
				if err != nil {
					t.Fatal("valid strict metadata result was rejected")
				}
				documentMetadataAssertIndexScope(t, index, ctx, ids, position == 1)
				switch position {
				case 0:
					documentMetadataAssertSummary(t, values)
				case 1:
					documentMetadataAssertScalars(t, values)
				case 2:
					documentMetadataAssertDirectory(t, values, ids["doc"])
				}
			}
			if f.snapshot() != before {
				t.Fatal("strict metadata decode changed SQL rows")
			}
		})
	}
	valid := documentMetadataScopeRows(ids, documentMetadataScopeJSON)[0]
	for _, test := range []struct {
		name string
		rows []map[string]interface{}
	}{
		{"foreign-document-row", []map[string]interface{}{{"id": ids["foreign-doc"], "kb_id": ids["kb"], "meta_fields": documentMetadataScopeJSON}}},
		{"wrong-dataset-row", []map[string]interface{}{{"id": ids["doc"], "kb_id": ids["foreign-kb"], "meta_fields": documentMetadataScopeJSON}}},
		{"inactive-document-row", []map[string]interface{}{{"id": ids["inactive-doc"], "kb_id": ids["kb"], "meta_fields": documentMetadataScopeJSON}}},
		{"missing-row-ID", []map[string]interface{}{{"kb_id": ids["kb"], "meta_fields": documentMetadataScopeJSON}}},
		{"duplicate-row", []map[string]interface{}{valid, valid}},
		{"trailing-JSON", documentMetadataScopeRows(ids, documentMetadataScopeJSON+` {}`)},
		{"truncated-prefix", documentMetadataScopeRows(ids, []byte{1, 2, 3})},
		{"invalid-prefix-length", documentMetadataScopeRows(ids, []byte{255, 255, 255, 255, '{', '}'})},
		{"valid-prefix-plus-garbage", documentMetadataScopeRows(ids, append(documentMetadataLengthPrefix(documentMetadataScopeJSON), 0))},
		{"non-finite-map", documentMetadataScopeRows(ids, map[string]interface{}{"value": math.NaN()})},
	} {
		t.Run(test.name, func(t *testing.T) {
			before := f.snapshot()
			index.rows = test.rows
			stale := test.name == "foreign-document-row" || test.name == "inactive-document-row"
			for position, call := range documentMetadataScopeCalls(svc, ctx, ids["normal"], ids["kb"], ids["doc"]) {
				index.calls = nil
				values, err := call()
				if stale && position != 1 {
					if err != nil || len(values) != 0 || len(index.calls) != 1 {
						t.Fatal("all-document directory did not filter an exact-KB stale SQL identity")
					}
				} else if err == nil || values != nil || len(index.calls) != 1 {
					t.Fatal("index scope mismatch or corrupt metadata was accepted or silently discarded")
				}
			}
			if stale {
				index.calls = nil
				if values, err := svc.GetMetadataSummary(ctx, ids["kb"], ids["normal"], []string{ids["doc"]}); err == nil || values != nil || len(index.calls) != 1 {
					t.Fatal("explicit summary selection accepted an unexpected document metadata row")
				}
			}
			if f.snapshot() != before {
				t.Fatal("rejected index result changed SQL rows")
			}
		})
	}
	t.Run("incomplete-or-nil-index-response", func(t *testing.T) {
		before := f.snapshot()
		index.rows = documentMetadataScopeRows(ids, documentMetadataScopeJSON)
		defer func() { index.total, index.nilReply = nil, false }()
		for _, incomplete := range []bool{true, false} {
			total := int64(2)
			index.total, index.nilReply = &total, !incomplete
			for _, call := range documentMetadataScopeCalls(svc, ctx, ids["normal"], ids["kb"], ids["doc"]) {
				index.calls = nil
				if values, err := call(); err == nil || values != nil || len(index.calls) == 0 {
					t.Fatal("incomplete or nil metadata index response became a successful directory")
				}
			}
		}
		if f.snapshot() != before {
			t.Fatal("incomplete metadata index response changed SQL rows")
		}
	})
	t.Run("index-failure-not-empty-success", func(t *testing.T) {
		before := f.snapshot()
		controlled := errors.New("controlled metadata index failure")
		index.err = controlled
		defer func() { index.err = nil }()
		for _, call := range documentMetadataScopeCalls(svc, ctx, ids["normal"], ids["kb"], ids["doc"]) {
			if values, err := call(); !errors.Is(err, controlled) || values != nil {
				t.Fatal("metadata index failure was converted into empty success")
			}
		}
		if f.snapshot() != before {
			t.Fatal("metadata index failure changed SQL rows")
		}
	})
	t.Run("all-documents-directory-spans-multiple-pages", func(t *testing.T) {
		const count = 1001
		const payload = `{"batch":"all-pages","zero":0}`
		bulkIDs := make([]string, 0, count)
		documents := make([]entity.Document, 0, count)
		rows := make([]map[string]interface{}, 0, count)
		name, location, status := "paged-source.bin", "paged-source.bin", "1"
		for position := 0; position < count; position++ {
			hash := sha256.Sum256([]byte(fmt.Sprintf("%s/metadata-page/%d", ids["doc"], position)))
			id := fmt.Sprintf("%x", hash)[:32]
			bulkIDs = append(bulkIDs, id)
			documents = append(documents, entity.Document{ID: id, KbID: ids["kb"], CreatedBy: ids["owner"], ParserID: "naive", ParserConfig: entity.JSONMap{},
				Type: "doc", SourceType: "local", Suffix: "bin", Name: &name, Location: &location, Status: &status})
			rows = append(rows, map[string]interface{}{"id": id, "kb_id": ids["kb"], "meta_fields": payload})
		}
		f.journal.Event("reserve-metadata-pagination-documents-before-insert", map[string]any{
			"document_ids": bulkIDs, "dataset": ids["kb"], "count": count, "metadata_sha256": fmt.Sprintf("%x", sha256.Sum256([]byte(payload))), "index_backend": "controlled paged Search",
		})
		var preexisting int
		if err := f.read.QueryRow("SELECT count(*) FROM t_ai_documents WHERE id=ANY($1)", bulkIDs).Scan(&preexisting); err != nil || preexisting != 0 {
			t.Fatal("owned metadata pagination document IDs must initially be absent")
		}
		t.Cleanup(func() {
			if err := f.db.Where("id IN ?", bulkIDs).Delete(&entity.Document{}).Error; err != nil {
				t.Error("owned metadata pagination document cleanup failed")
			}
			var remaining int
			if err := f.read.QueryRow("SELECT count(*) FROM t_ai_documents WHERE id=ANY($1)", bulkIDs).Scan(&remaining); err != nil || remaining != 0 {
				t.Error("independent metadata pagination document cleanup readback failed")
			}
			f.journal.Event("metadata-pagination-documents-cleanup-independent-readback", map[string]any{"document_ids": bulkIDs, "remaining": remaining})
		})
		if err := f.db.CreateInBatches(&documents, 100).Error; err != nil {
			t.Fatal("owned metadata pagination document creation failed")
		}
		before := f.snapshot()
		index.rows = rows
		sort.Strings(bulkIDs)
		for _, summary := range []bool{true, false} {
			index.calls = nil
			var values map[string]interface{}
			var err error
			if summary {
				values, err = svc.GetMetadataSummary(ctx, ids["kb"], ids["normal"], nil)
			} else {
				values, err = svc.GetMetadataByKBs(ctx, ids["normal"], []string{ids["kb"]})
			}
			if err != nil || len(index.calls) != 2 {
				t.Fatal("all-document metadata directory stopped at its first page")
			}
			for page, call := range index.calls {
				_, restricted := call.request.Filter["id"]
				if call.ctx != ctx || call.request.Offset != page*1000 || call.request.Limit != 1000 || restricted || !call.request.IncludeUnavailable ||
					!reflect.DeepEqual(call.request.IndexNames, []string{BuildMetadataIndexName(ids["owner"])}) || !reflect.DeepEqual(call.request.KbIDs, []string{ids["kb"]}) ||
					call.request.OrderBy == nil || !reflect.DeepEqual(call.request.OrderBy.Fields, []types.OrderByField{{Field: "kb_id", Type: types.SortAsc}, {Field: "id", Type: types.SortAsc}}) {
					t.Fatal("metadata pagination changed request context or dataset/order scope")
				}
			}
			raw, encodeErr := json.Marshal(values)
			if encodeErr != nil {
				t.Fatal("paged metadata response could not be encoded")
			}
			if summary {
				var result map[string]struct {
					Type   string
					Values [][2]json.RawMessage
				}
				if json.Unmarshal(raw, &result) != nil {
					t.Fatal("paged metadata summary contract changed")
				}
				for _, field := range []string{"batch", "zero"} {
					var actualCount int
					entry := result[field]
					if len(entry.Values) != 1 || json.Unmarshal(entry.Values[0][1], &actualCount) != nil || actualCount != count {
						t.Fatal("paged metadata summary did not include every authorized document")
					}
				}
				if result["zero"].Type != "number" {
					t.Fatal("paged metadata summary changed a valid numeric zero")
				}
			} else {
				var directory map[string]map[string][]string
				if json.Unmarshal(raw, &directory) != nil {
					t.Fatal("paged metadata directory contract changed")
				}
				for _, selected := range [][]string{directory["batch"]["all-pages"], directory["zero"]["0"]} {
					sort.Strings(selected)
					if !reflect.DeepEqual(selected, bulkIDs) {
						t.Fatal("paged metadata directory lost or duplicated an authorized document")
					}
				}
			}
		}
		if f.snapshot() != before {
			t.Fatal("paged metadata directory reads changed SQL rows")
		}
		f.journal.Event("metadata-pagination-business-independent-readback", map[string]any{"count": count, "pages_per_read": 2, "batch_size": 1000, "summary_complete": true, "directory_complete": true, "SQL_unchanged_by_reads": true})
	})
	f.journal.Event("document-metadata-strict-result-independent-readback", map[string]any{
		"strict_length_prefix": true, "large_JSON_number_exact": true, "zero_false_empty_preserved": true,
		"stale_rows_filtered_for_all_rejected_for_explicit": true, "wrong_KB_and_duplicate_rows_rejected": true, "invalid_JSON_and_nonfinite_rejected": true, "SQL_unchanged_by_reads": true, "real_index_backend": false,
	})
}
