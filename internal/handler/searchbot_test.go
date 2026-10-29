package handler

import (
	"context"
	"encoding/json"
	"errors"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"

	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/service"
)

type fakeRelatedQuestions struct {
	calls                      int
	ctx                        context.Context
	userID, question, searchID string
	result                     []string
	err                        error
}

func (f *fakeRelatedQuestions) RelatedQuestions(ctx context.Context, userID, question, searchID string) ([]string, error) {
	f.calls++
	f.ctx, f.userID, f.question, f.searchID = ctx, userID, question, searchID
	return f.result, f.err
}

func TestSearchbotRejectsInvalidWithoutServiceCall(t *testing.T) {
	for _, body := range []string{`{}`, `null`, `[]`, `{"question":null}`, `{"question":7}`, `{"question":" "}`, `{"question":"q","search_id":null}`, `{"question":"q","search_id":3}`, `{"question":"q","search_id":" "}`, `{"question":"q","extra":true}`, `{"question":"q","question":"second"}`, `{"question":"q"} {}`} {
		t.Run(body, func(t *testing.T) {
			fake := &fakeRelatedQuestions{}
			c, w := searchbotContext(body)
			NewSearchbotHandler(fake).Handle(c)
			if got := searchbotCode(t, w); got != common.CodeArgumentError || fake.calls != 0 {
				t.Fatalf("code=%d service calls=%d", got, fake.calls)
			}
		})
	}
	fake := &fakeRelatedQuestions{}
	c, w := searchbotContext(`{"question":"q"}`)
	c.Set("user", (*entity.User)(nil))
	NewSearchbotHandler(fake).Handle(c)
	if searchbotCode(t, w) != common.CodeUnauthorized || fake.calls != 0 {
		t.Fatal("unauthenticated call reached service")
	}
}

func TestSearchbotUsesRequestContextAndSanitizesErrors(t *testing.T) {
	for _, tc := range []struct {
		err  error
		code common.ErrorCode
	}{
		{nil, common.CodeSuccess}, {service.ErrSearchbotArgument, common.CodeArgumentError}, {service.ErrSearchbotAccessDenied, common.CodeAuthenticationError}, {errors.New("private-key-provider-body"), common.CodeOperatingError}, {context.Canceled, common.CodeOperatingError},
	} {
		fake := &fakeRelatedQuestions{err: tc.err}
		c, w := searchbotContext(`{"question":"question","search_id":"search"}`)
		ctx, cancel := context.WithCancel(c.Request.Context())
		defer cancel()
		c.Request = c.Request.WithContext(ctx)
		NewSearchbotHandler(fake).Handle(c)
		if searchbotCode(t, w) != tc.code || fake.calls != 1 || fake.ctx != ctx || fake.userID != "actor" || fake.question != "question" || fake.searchID != "search" {
			t.Fatalf("incorrect dispatch: %s", w.Body.String())
		}
		if strings.Contains(w.Body.String(), "private-key") {
			t.Fatal("error leaked provider data")
		}
		if tc.err == nil && !strings.Contains(w.Body.String(), `"data":[]`) {
			t.Fatal("empty questions must encode []")
		}
	}
}

func searchbotContext(body string) (*gin.Context, *httptest.ResponseRecorder) {
	gin.SetMode(gin.TestMode)
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest("POST", "/api/v1/searchbots/related_questions", strings.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")
	c.Set("user", &entity.User{ID: "actor"})
	return c, w
}

func searchbotCode(t *testing.T, w *httptest.ResponseRecorder) common.ErrorCode {
	t.Helper()
	var response struct {
		Code common.ErrorCode `json:"code"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	return response.Code
}

type fakeSearchbotRetriever struct {
	calls   int
	ctx     context.Context
	userID  string
	request *service.RetrievalTestRequest
	result  *service.RetrievalTestResponse
	err     error
}

func (f *fakeSearchbotRetriever) RetrievalTest(ctx context.Context, req *service.RetrievalTestRequest, userID string) (*service.RetrievalTestResponse, error) {
	f.calls++
	f.ctx, f.request, f.userID = ctx, req, userID
	return f.result, f.err
}

func TestSearchbotRetrievalRejectsScopeAndStrictJSON(t *testing.T) {
	for _, body := range []string{
		`{}`, `null`, `[]`, `{"question":"q"}`, `{"kb_id":null,"question":"q"}`, `{"kb_id":[],"question":"q"}`,
		`{"kb_id":" ","question":"q"}`, `{"kb_id":["kb",""],"question":"q"}`, `{"kb_id":["kb",null],"question":"q"}`,
		`{"kb_id":1,"question":"q"}`, `{"kb_id":"kb","question":null}`, `{"kb_id":"kb","question":" "}`,
		`{"kb_id":"kb","question":"q","kb_id":"foreign"}`, `{"kb_id":"kb","question":"q"} {}`,
		`{"dataset_ids":["kb"],"question":"q"}`, `{"kb_id":"kb","question":"q","page":0}`,
		`{"kb_id":"kb","question":"q","size":-1}`, `{"kb_id":"kb","question":"q","top_k":-1}`,
		`{"kb_id":"kb","question":"q","top_k":2049}`, `{"kb_id":"kb","question":"q","top_k":null}`,
		`{"kb_id":"kb","question":"q","tenant_rerank_id":"9007199254740993"}`,
		`{"kb_id":"kb","question":"q","tenant_rerank_id":1.1}`, `{"kb_id":"kb","question":"q","similarity_threshold":2}`,
		`{"kb_id":"kb","question":"q","keyword":null}`, `{"kb_id":"kb","question":"q","cross_languages":[1]}`,
		`{"kb_id":"kb","question":"q","doc_ids":[""]}`, `{"kb_id":"kb","question":"q","search_id":" "}`,
		`{"kb_id":"kb","question":"q","unexpected":true}`,
	} {
		t.Run(body, func(t *testing.T) {
			f := &fakeSearchbotRetriever{}
			c, w := searchbotContext(body)
			NewSearchbotHandler(nil, f).RetrievalTest(c)
			if w.Code != 400 || searchbotCode(t, w) != common.CodeBadRequest || f.calls != 0 {
				t.Fatalf("status=%d response=%s calls=%d", w.Code, w.Body, f.calls)
			}
		})
	}
	f := &fakeSearchbotRetriever{}
	c, w := searchbotContext(`{"kb_id":"kb","question":"q"}`)
	c.Set("user", (*entity.User)(nil))
	NewSearchbotHandler(nil, f).RetrievalTest(c)
	if searchbotCode(t, w) != common.CodeUnauthorized || f.calls != 0 {
		t.Fatal("nil actor reached retrieval")
	}
}

func TestSearchbotRetrievalMapsExactControlsAndDefaults(t *testing.T) {
	for _, scope := range []string{`"kb"`, `["kb"]`} {
		f := &fakeSearchbotRetriever{result: &service.RetrievalTestResponse{}}
		c, w := searchbotContext(`{"kb_id":` + scope + `,"question":"q"}`)
		NewSearchbotHandler(nil, f).RetrievalTest(c)
		r := f.request
		if searchbotCode(t, w) != common.CodeSuccess || f.calls != 1 || len(r.DatasetIDs) != 1 || r.DatasetIDs[0] != "kb" || r.DocIDs != nil || *r.Page != 1 || *r.Size != 30 || *r.TopK != 1024 || *r.UseKG || *r.Keyword || *r.SimilarityThreshold != 0 || *r.VectorSimilarityWeight != 0.3 {
			t.Fatalf("defaults: %#v %s", r, w.Body)
		}
	}
	f := &fakeSearchbotRetriever{result: &service.RetrievalTestResponse{}}
	c, w := searchbotContext(`{"kb_id":["a","b"],"question":"q","page":2,"size":7,"top_k":0,"doc_ids":[],"cross_languages":[],"tenant_rerank_id":9007199254740993,"keyword":false,"use_kg":false,"similarity_threshold":0,"vector_similarity_weight":0,"highlight":true,"search_id":"saved","meta_data_filter":{"manual":[{"value":9007199254740993}]}}`)
	ctx, cancel := context.WithCancel(c.Request.Context())
	defer cancel()
	c.Request = c.Request.WithContext(ctx)
	NewSearchbotHandler(nil, f).RetrievalTest(c)
	r := f.request
	if searchbotCode(t, w) != common.CodeSuccess || f.ctx != ctx || f.userID != "actor" || len(r.DatasetIDs) != 2 || *r.Page != 2 || *r.Size != 7 || *r.TopK != 0 || r.DocIDs == nil || len(r.DocIDs) != 0 || r.CrossLanguages == nil || *r.TenantRerankID != 9007199254740993 || *r.VectorSimilarityWeight != 0 || *r.Keyword || *r.UseKG || !*r.Highlight || *r.SearchID != "saved" {
		t.Fatalf("controls lost: %#v %s", r, w.Body)
	}
	if r.Filter["manual"].([]interface{})[0].(map[string]interface{})["value"] != json.Number("9007199254740993") {
		t.Fatal("metadata number rounded")
	}
	f = &fakeSearchbotRetriever{result: &service.RetrievalTestResponse{}}
	c, w = searchbotContext(`{"kb_id":"kb","question":"q","doc_ids":null}`)
	NewSearchbotHandler(nil, f).RetrievalTest(c)
	if searchbotCode(t, w) != common.CodeSuccess || f.request.DocIDs != nil {
		t.Fatal("null doc scope changed semantics")
	}
}

func TestSearchbotRetrievalErrorsAreNotSuccessOrPrivateData(t *testing.T) {
	for _, tc := range []struct {
		err    error
		code   common.ErrorCode
		status int
	}{
		{service.ErrRetrievalArgument, common.CodeBadRequest, 400},
		{service.ErrSearchbotArgument, common.CodeBadRequest, 400},
		{service.ErrDatasetAccessDenied, common.CodeForbidden, 200},
		{service.ErrSearchbotAccessDenied, common.CodeForbidden, 200},
		{context.Canceled, common.CodeServerError, 500},
		{errors.New("private-key-provider-body"), common.CodeServerError, 500},
	} {
		f := &fakeSearchbotRetriever{err: tc.err}
		c, w := searchbotContext(`{"kb_id":"kb","question":"q"}`)
		NewSearchbotHandler(nil, f).RetrievalTest(c)
		if searchbotCode(t, w) != tc.code || w.Code != tc.status || f.calls != 1 || strings.Contains(w.Body.String(), "private-key") {
			t.Fatalf("%s", w.Body)
		}
	}
	c, w := searchbotContext(`{"kb_id":"kb","question":"q"}`)
	NewSearchbotHandler(nil).RetrievalTest(c)
	if w.Code != 503 || searchbotCode(t, w) == common.CodeSuccess {
		t.Fatal("unwired retrieval returned success")
	}
}
