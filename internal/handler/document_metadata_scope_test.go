package handler

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/entity"
	"multirag/internal/service"
)

type metadataContextKey struct{}

type fakeDocumentMetadataReader struct {
	calls                            []string
	ctx                              context.Context
	kbID, userID, docID              string
	docIDs                           []string
	page, pageSize                   int
	listErr, metadataErr, summaryErr error
}

func (f *fakeDocumentMetadataReader) ListDocumentsByKBID(ctx context.Context, kbID, userID string, page, pageSize int) ([]*service.DocumentResponse, int64, error) {
	f.calls = append(f.calls, "list")
	f.ctx = ctx
	f.kbID = kbID
	f.userID = userID
	f.page = page
	f.pageSize = pageSize
	return []*service.DocumentResponse{{ID: "doc-1", KbID: kbID}}, 1, f.listErr
}
func (f *fakeDocumentMetadataReader) GetDocumentMetadataByID(ctx context.Context, docID, userID string) (map[string]interface{}, error) {
	f.calls = append(f.calls, "metadata")
	f.ctx = ctx
	f.docID = docID
	f.userID = userID
	return map[string]interface{}{"zero": json.Number("0"), "big": json.Number("9007199254740993")}, f.metadataErr
}
func (f *fakeDocumentMetadataReader) GetMetadataSummary(ctx context.Context, kbID, userID string, docIDs []string) (map[string]interface{}, error) {
	f.calls = append(f.calls, "summary")
	f.ctx = ctx
	f.kbID = kbID
	f.userID = userID
	f.docIDs = docIDs
	return map[string]interface{}{"summary": "ready"}, f.summaryErr
}
func legacyMetadataHTTP(t *testing.T, f *fakeDocumentMetadataReader, identity interface{}, path, body string) *httptest.ResponseRecorder {
	t.Helper()
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(func(c *gin.Context) {
		if identity != nil {
			c.Set("user", identity)
		}
	})
	h := &DocumentHandler{metadataService: f}
	r.POST("/document/list", h.ListDocumentsByKB)
	r.POST("/document/metadata/summary", h.MetadataSummary)
	request := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
	request.Header.Set("Content-Type", "application/json")
	request = request.WithContext(context.WithValue(request.Context(), metadataContextKey{}, "request-scope"))
	response := httptest.NewRecorder()
	r.ServeHTTP(response, request)
	return response
}
func TestLegacyDocumentMetadataRequiresActorBeforeService(t *testing.T) {
	for _, identity := range []interface{}{nil, "user", (*entity.User)(nil), &entity.User{}} {
		for _, request := range []struct{ path, body string }{{"/document/list?id=kb-1", ""}, {"/document/metadata/summary", `{"kb_id":"kb-1"}`}} {
			f := &fakeDocumentMetadataReader{}
			response := legacyMetadataHTTP(t, f, identity, request.path, request.body)
			var envelope struct {
				Code int `json:"code"`
			}
			if err := json.Unmarshal(response.Body.Bytes(), &envelope); err != nil {
				t.Fatal(err)
			}
			if envelope.Code == 0 || len(f.calls) != 0 {
				t.Fatalf("unauthenticated legacy metadata reached service: %s calls=%v", response.Body.String(), f.calls)
			}
		}
	}
}
func TestLegacyDocumentListForwardsActorAndRequestContext(t *testing.T) {
	f := &fakeDocumentMetadataReader{}
	response := legacyMetadataHTTP(t, f, &entity.User{ID: "actor-1"}, "/document/list?id=kb-1&page=2&page_size=7", "")
	if response.Code != http.StatusOK || f.kbID != "kb-1" || f.userID != "actor-1" || f.docID != "doc-1" || f.page != 2 || f.pageSize != 7 || f.ctx.Value(metadataContextKey{}) != "request-scope" || strings.Join(f.calls, ",") != "list,metadata" {
		t.Fatalf("legacy list lost scope: status=%d calls=%+v", response.Code, f)
	}
	if !strings.Contains(response.Body.String(), `"big":9007199254740993`) || !strings.Contains(response.Body.String(), `"zero":0`) {
		t.Fatalf("metadata numeric values changed: %s", response.Body.String())
	}
}
func TestLegacyDocumentListDoesNotSwallowMetadataFailure(t *testing.T) {
	for _, problem := range []error{service.ErrDatasetAccessDenied, service.ErrChunkNotFound, errors.New("private engine detail password")} {
		f := &fakeDocumentMetadataReader{metadataErr: problem}
		response := legacyMetadataHTTP(t, f, &entity.User{ID: "actor-1"}, "/document/list?kb_id=kb-1", "")
		var envelope struct {
			Code int             `json:"code"`
			Data json.RawMessage `json:"data"`
		}
		if err := json.Unmarshal(response.Body.Bytes(), &envelope); err != nil {
			t.Fatal(err)
		}
		if response.Code == http.StatusOK || envelope.Code == 0 || string(envelope.Data) != "null" || strings.Contains(response.Body.String(), "private engine") || strings.Contains(response.Body.String(), "doc-1") {
			t.Fatalf("legacy list turned failed metadata into success/partial data: %d %s", response.Code, response.Body.String())
		}
	}
	f := &fakeDocumentMetadataReader{listErr: service.ErrDatasetAccessDenied}
	legacyMetadataHTTP(t, f, &entity.User{ID: "actor-1"}, "/document/list?id=foreign", "")
	if strings.Join(f.calls, ",") != "list" {
		t.Fatalf("denied list still read metadata: %v", f.calls)
	}
}
func TestLegacyMetadataSummaryPreservesSelectionAndActor(t *testing.T) {
	for _, body := range []string{`{"kb_id":"kb-1"}`, `{"kb_id":"kb-1","doc_ids":[]}`, `{"kb_id":"kb-1","doc_ids":["doc-1"]}`} {
		f := &fakeDocumentMetadataReader{}
		response := legacyMetadataHTTP(t, f, &entity.User{ID: "actor-1"}, "/document/metadata/summary", body)
		if response.Code != http.StatusOK || f.kbID != "kb-1" || f.userID != "actor-1" || f.ctx.Value(metadataContextKey{}) != "request-scope" {
			t.Fatalf("summary scope changed: status=%d reader=%+v", response.Code, f)
		}
		if !strings.Contains(body, "doc_ids") && f.docIDs != nil {
			t.Fatal("omitted document selection became explicit")
		}
		if strings.Contains(body, `"doc_ids":[]`) && (f.docIDs == nil || len(f.docIDs) != 0) {
			t.Fatal("explicit empty document selection was broadened")
		}
		if strings.Contains(body, "doc-1") && (len(f.docIDs) != 1 || f.docIDs[0] != "doc-1") {
			t.Fatal("explicit document selection changed")
		}
	}
}
func TestLegacyMetadataSummaryErrorsAreOpaque(t *testing.T) {
	f := &fakeDocumentMetadataReader{summaryErr: errors.New("private engine detail password")}
	response := legacyMetadataHTTP(t, f, &entity.User{ID: "actor-1"}, "/document/metadata/summary", `{"kb_id":"kb-1"}`)
	if response.Code != http.StatusInternalServerError || strings.Contains(response.Body.String(), "private engine") || strings.Contains(response.Body.String(), "password") {
		t.Fatalf("summary error leaked backend details: %d %s", response.Code, response.Body.String())
	}
}

func datasetMetadataHTTP(f *fakeDocumentMetadataReader, identity any, raw string, ctx context.Context) *httptest.ResponseRecorder {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(func(c *gin.Context) {
		if identity != nil {
			c.Set("user", identity)
		}
	})
	h := &DocumentHandler{metadataService: f}
	r.GET("/api/v1/datasets/:dataset_id/metadata/summary", h.MetadataSummaryByDataset)
	request := httptest.NewRequest(http.MethodGet, "/api/v1/datasets/kb-1/metadata/summary", nil)
	request.URL.RawQuery = raw
	w := httptest.NewRecorder()
	r.ServeHTTP(w, request.WithContext(ctx))
	return w
}

func TestDatasetMetadataSummarySelectionAndContext(t *testing.T) {
	for _, raw := range []string{"", "doc_ids=", "doc_ids=doc-1%2Cdoc-2"} {
		f := &fakeDocumentMetadataReader{}
		ctx := context.WithValue(context.Background(), metadataContextKey{}, "request-scope")
		w := datasetMetadataHTTP(f, &entity.User{ID: "actor-1"}, raw, ctx)
		if w.Code != 200 || f.ctx != ctx || f.kbID != "kb-1" || f.userID != "actor-1" || strings.Join(f.calls, ",") != "summary" {
			t.Fatalf("scope lost: %d %+v", w.Code, f)
		}
		if strings.Contains(raw, "doc-1") {
			if strings.Join(f.docIDs, ",") != "doc-1,doc-2" {
				t.Fatal("selection changed")
			}
		} else if f.docIDs != nil {
			t.Fatal("empty CSV must match Python all-document selection")
		}
		if !strings.Contains(w.Body.String(), `"data":{"summary":{"summary":"ready"}}`) {
			t.Fatalf("wrong envelope: %s", w.Body)
		}
	}
}

func TestDatasetMetadataSummaryRejectsAmbiguousQueryBeforeReader(t *testing.T) {
	for _, raw := range []string{"doc_ids=%zz", "doc_ids=a;doc_ids=b", "doc_ids=a&doc_ids=b", "doc_ids=a,", "doc_ids=,", "doc_ids=%20", "doc_ids=a,%20b", "doc_id=a"} {
		f := &fakeDocumentMetadataReader{}
		w := datasetMetadataHTTP(f, &entity.User{ID: "actor"}, raw, context.Background())
		var response struct {
			Code int `json:"code"`
		}
		if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil {
			t.Fatal(err)
		}
		if response.Code == 0 || len(f.calls) != 0 {
			t.Fatalf("malformed query reached reader %q: %s", raw, w.Body)
		}
	}
}

func TestDatasetMetadataSummaryAuthAndFailures(t *testing.T) {
	for _, identity := range []any{nil, "user", (*entity.User)(nil), &entity.User{}} {
		f := &fakeDocumentMetadataReader{}
		w := datasetMetadataHTTP(f, identity, "", context.Background())
		if strings.Contains(w.Body.String(), `"code":0`) || len(f.calls) != 0 {
			t.Fatalf("unauthorized read: %s", w.Body)
		}
	}
	for _, problem := range []struct {
		err    error
		status int
	}{{service.ErrDatasetAccessDenied, 403}, {service.ErrChunkNotFound, 404}, {context.Canceled, 500}, {errors.New("private SQL password"), 500}} {
		f := &fakeDocumentMetadataReader{summaryErr: problem.err}
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		w := datasetMetadataHTTP(f, &entity.User{ID: "actor"}, "doc_ids=doc", ctx)
		if w.Code != problem.status || f.ctx != ctx || strings.Contains(w.Body.String(), "private") || !strings.Contains(w.Body.String(), `"data":null`) {
			t.Fatalf("failure changed: %d %s", w.Code, w.Body)
		}
	}
}
