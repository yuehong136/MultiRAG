package handler_test

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/gin-gonic/gin"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/service"
	"multirag/internal/service/nlp"
	"multirag/internal/testsupport"
	"net/http"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

type canonicalChatRetrieval struct {
	calls atomic.Int64
	mode  atomic.Int32
	t     *testing.T
}

func (r *canonicalChatRetrieval) Retrieval(ctx context.Context, req *nlp.RetrievalRequest) (*nlp.RetrievalResult, error) {
	r.calls.Add(1)
	if req.Question != "question" || !reflect.DeepEqual(req.TenantIDs, []string{"owner-tenant"}) || !reflect.DeepEqual(req.KbIDs, []string{"rag-kb"}) || !reflect.DeepEqual(req.DocIDs, []string{"selected-doc"}) || req.Top == nil || *req.Top != 0 || req.SimilarityThreshold == nil || *req.SimilarityThreshold != 0 || req.VectorSimilarityWeight == nil || *req.VectorSimilarityWeight != 0 || req.EmbeddingModel == nil {
		r.t.Error("canonical retrieval options or scope changed", req)
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if r.mode.Load() == 1 {
		return &nlp.RetrievalResult{}, nil
	}
	if r.mode.Load() == 2 {
		return nil, errors.New("controlled retrieval unavailable")
	}
	return &nlp.RetrievalResult{Chunks: []map[string]any{{"id": "chunk-one", "doc_id": "selected-doc", "docnm_kwd": "owned.txt", "content_with_weight": "approved knowledge"}}, DocAggs: []map[string]any{{"doc_id": "selected-doc", "doc_name": "owned.txt"}}}, nil
}

// The original ChatSession target performs RAG through Retrieval. This keeps
// the real authenticated HTTP/SQL/model driver path and controls that one peer.
func TestAfter10bbeAskChatRAGJWTSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	var chatCalls atomic.Int64
	upstream := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		chatCalls.Add(1)
		if r.Header.Get("Authorization") != "Bearer rag-chat-key" {
			t.Error("RAG chat binding crossed instance")
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		messages, _ := body["messages"].([]any)
		if body["model"] != "rag-chat" || len(messages) != 2 || messages[0].(map[string]any)["content"] != "Rules [1] owned.txt\napproved knowledge" || messages[1].(map[string]any)["content"] != "question" {
			t.Error("retrieved system knowledge or user history changed", body)
		}
		if body["temperature"] != float64(0) || body["top_p"] != float64(0) {
			t.Error("stored zero options lost")
		}
		if body["stream"] == true {
			fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"grounded answer\"}}]}\n\ndata: [DONE]\n\n")
		} else {
			fmt.Fprint(w, `{"choices":[{"message":{"content":"grounded answer"}}]}`)
		}
	}))
	f.enroll(t, "vllm", "rag-chat", "rag-chat-key", upstream.URL, "rag-chat", "chat")
	f.enroll(t, "vllm", "rag-embed", "rag-embed-key", upstream.URL, "rag-embed", "embedding")
	active, owner := "1", "owner"
	if err := dao.DB.Create(&entity.Knowledgebase{ID: "rag-kb", TenantID: "owner-tenant", CreatedBy: "owner", Permission: "team", Name: "owned", EmbdID: "rag-embed@rag-embed@vllm", Status: &active, ParserConfig: entity.JSONMap{}}).Error; err != nil {
		t.Fatal(err)
	}
	if err := dao.DB.Create(&entity.Chat{ID: "rag-dialog", TenantID: "owner-tenant", LLMID: "rag-chat@rag-chat@vllm", LLMSetting: entity.JSONMap{"temperature": 0, "top_p": 0}, PromptConfig: entity.JSONMap{"system": "Rules {knowledge}", "empty_response": "No matching knowledge."}, KBIDs: entity.JSONSlice{"rag-kb"}, Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	if err := dao.DB.Model(&entity.Chat{}).Where("id=?", "rag-dialog").Updates(map[string]any{"top_k": 0, "similarity_threshold": 0, "vector_similarity_weight": 0}).Error; err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"grounded", "streamed", "empty", "failed", "foreign-kb"} {
		if err := dao.DB.Create(&entity.ChatSession{ID: id, DialogID: "rag-dialog", UserID: &owner, Message: json.RawMessage(`{"messages":[]}`), Reference: json.RawMessage(`[]`)}).Error; err != nil {
			t.Fatal(err)
		}
	}
	retrieval := &canonicalChatRetrieval{t: t}
	app := gin.New()
	app.Use(gin.Recovery())
	h := handler.NewChatSessionHandler(service.NewChatSessionServiceWithRetrieval(retrieval), service.NewUserService())
	app.POST("/v1/conversation/completion", handler.NewAuthHandler().AuthMiddleware(), h.Completion)
	api := testsupport.NewHTTP(t, app)
	f.url, f.client = api.URL, api.Client()
	body := func(id string, stream bool) map[string]any {
		return map[string]any{"conversation_id": id, "stream": stream, "messages": []any{map[string]any{"role": "user", "content": "question", "doc_ids": []string{"selected-doc"}, "id": "message"}}}
	}
	read := func(id, column string) string {
		t.Helper()
		var value string
		if err := f.Read.QueryRow("SELECT "+column+" FROM conversation WHERE id=?", id).Scan(&value); err != nil {
			t.Fatal(err)
		}
		return value
	}
	before := f.snapshot(t)
	for _, who := range []string{"", "foreign", "inactive", "invited", "revoked", "member", "disabledtenant"} {
		_, failed, _ := f.request(t, who, "POST", "/v1/conversation/completion", body("grounded", false))
		if providerEnvelopeError(failed) == nil || chatCalls.Load() != 0 || retrieval.calls.Load() != 0 || f.snapshot(t) != before {
			t.Fatal("denied RAG touched retrieval, supplier or SQL", who)
		}
	}
	result := f.requireSuccess(t, "owner", "POST", "/v1/conversation/completion", body("grounded", false))
	if result["data"].(map[string]any)["answer"] != "grounded answer" || !strings.Contains(read("grounded", "reference"), "chunk-one") || !strings.Contains(read("grounded", "message"), "grounded answer") {
		t.Fatal("grounded answer/reference did not persist")
	}
	_, _, raw := f.request(t, "owner", "POST", "/v1/conversation/completion", body("streamed", true))
	if !strings.Contains(raw, `"data":true`) || strings.Contains(read("streamed", "message"), "[DONE]") || !strings.Contains(read("streamed", "reference"), "chunk-one") {
		t.Fatal("RAG stream or persisted reference changed", raw)
	}
	retrieval.mode.Store(1)
	count := chatCalls.Load()
	result = f.requireSuccess(t, "owner", "POST", "/v1/conversation/completion", body("empty", false))
	if result["data"].(map[string]any)["answer"] != "No matching knowledge." || chatCalls.Load() != count {
		t.Fatal("empty retrieval reached supplier or lost configured response")
	}
	retrieval.mode.Store(2)
	_, failed, _ := f.request(t, "owner", "POST", "/v1/conversation/completion", body("failed", false))
	if providerEnvelopeError(failed) == nil || chatCalls.Load() != count || read("failed", "message") != `{"messages":[]}` {
		t.Fatal("retrieval failure persisted or reached supplier")
	}
	if err := dao.DB.Model(&entity.Knowledgebase{}).Where("id=?", "rag-kb").Update("tenant_id", "foreign-tenant").Error; err != nil {
		t.Fatal(err)
	}
	before = f.snapshot(t)
	retrievalCount := retrieval.calls.Load()
	_, failed, _ = f.request(t, "owner", "POST", "/v1/conversation/completion", body("foreign-kb", false))
	if providerEnvelopeError(failed) == nil || chatCalls.Load() != count || retrieval.calls.Load() != retrievalCount || f.snapshot(t) != before {
		t.Fatal("foreign KB reached retrieval or changed SQL")
	}
}

type canonicalAskRetriever struct {
	calls atomic.Int64
	mode  atomic.Int32
	t     *testing.T
}

func (r *canonicalAskRetriever) RetrievalTest(req *service.RetrievalTestRequest, actor string) (*service.RetrievalTestResponse, error) {
	r.calls.Add(1)
	if (actor != "owner" && actor != "member") || req.Question != "question" || !reflect.DeepEqual([]string(req.Datasets), []string{"ask-kb"}) || req.TopK == nil || *req.TopK != service.DefaultAskTopK {
		r.t.Error("original Ask retrieval scope/defaults changed")
	}
	if r.mode.Load() == 1 {
		return &service.RetrievalTestResponse{}, nil
	}
	if r.mode.Load() == 2 {
		return nil, errors.New("private-retrieval-failure")
	}
	return &service.RetrievalTestResponse{Chunks: []map[string]any{{"id": "ask-chunk", "doc_id": "ask-doc", "docnm_kwd": "owned.txt", "content_with_weight": "approved ask knowledge"}}, DocAggs: []map[string]any{{"doc_id": "ask-doc", "doc_name": "owned.txt"}}}, nil
}

func TestCanonicalAskHTTPAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	var calls atomic.Int64
	var mode atomic.Int32
	stopped := make(chan struct{}, 1)
	upstream := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("Authorization") != "Bearer ask-key" || !strings.HasSuffix(r.URL.Path, "/chat/completions") {
			t.Error("Ask model instance binding changed")
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		messages, _ := body["messages"].([]any)
		if body["model"] != "ask-chat" || body["stream"] != true || len(messages) != 2 || !strings.Contains(messages[0].(map[string]any)["content"].(string), "approved ask knowledge") || messages[1].(map[string]any)["content"] != "question" {
			t.Error("original Ask system knowledge or user message changed", body)
		}
		if mode.Load() == 3 {
			w.WriteHeader(503)
			fmt.Fprint(w, "private-provider-body")
			return
		}
		select {
		case <-time.After(100 * time.Millisecond):
		case <-r.Context().Done():
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"grounded answer\"}}]}\n\n")
		w.(http.Flusher).Flush()
		if mode.Load() == 4 {
			return
		}
		if mode.Load() == 5 {
			<-r.Context().Done()
			stopped <- struct{}{}
			return
		}
		fmt.Fprint(w, "data: [DONE]\n\n")
	}))
	f.enroll(t, "vllm", "ask-instance", "ask-key", upstream.URL, "ask-chat", "chat")
	active := "1"
	if err := dao.DB.Create(&entity.Search{ID: "ask-search", TenantID: "owner-tenant", CreatedBy: "owner", Name: "owned", Status: &active, SearchConfig: entity.JSONMap{"chat_id": "ask-chat@ask-instance@vllm"}}).Error; err != nil {
		t.Fatal(err)
	}
	retriever := &canonicalAskRetriever{t: t}
	realLLM := &handler.SearchBotRealLLM{Svc: service.NewModelProviderService()}
	h := handler.NewSearchBotHandler(service.NewSearchService(), nil, realLLM, nil)
	h.SetStreamLLM(realLLM)
	h.SetAskService(service.NewAskService(retriever, nil, 4096, 1))
	app := gin.New()
	app.Use(gin.Recovery())
	app.POST("/api/v1/searchbots/ask", handler.NewAuthHandler().AuthMiddleware(), func(c *gin.Context) {
		_ = http.NewResponseController(c.Writer).SetWriteDeadline(time.Now().Add(40 * time.Millisecond))
		h.Ask(c)
	})
	api := testsupport.NewHTTP(t, app)
	f.url, f.client = api.URL, api.Client()
	body := map[string]any{"question": "question", "kb_ids": []string{"ask-kb"}, "search_id": "ask-search"}
	before := f.snapshot(t)
	for _, who := range []string{"", "foreign", "inactive", "revoked", "invited", "disabledtenant"} {
		_, failed, raw := f.request(t, who, "POST", "/api/v1/searchbots/ask", body)
		if providerEnvelopeError(failed) == nil || calls.Load() != 0 || retriever.calls.Load() != 0 || strings.Contains(raw, `"data":true`) {
			t.Fatal("denied Ask reached retrieval or supplier", who)
		}
	}
	for _, who := range []string{"owner", "member"} {
		_, _, raw := f.request(t, who, "POST", "/api/v1/searchbots/ask", body)
		if !strings.Contains(raw, "grounded answer") || !strings.Contains(raw, "ask-chunk") || strings.Contains(raw, "[DONE]") || strings.Count(strings.ReplaceAll(raw, " ", ""), `"data":true`) != 1 {
			t.Fatal("original Ask answer, reference, or unique terminal lost", raw)
		}
	}
	count := calls.Load()
	for _, state := range []int32{1, 2} {
		retriever.mode.Store(state)
		_, _, raw := f.request(t, "owner", "POST", "/api/v1/searchbots/ask", body)
		if strings.Contains(strings.ReplaceAll(raw, " ", ""), `"data":true`) || strings.Contains(raw, `"final":true`) || strings.Contains(raw, "private-retrieval-failure") || calls.Load() != count {
			t.Fatal("empty/failed retrieval fabricated Ask success", raw)
		}
	}
	retriever.mode.Store(0)
	for _, state := range []int32{3, 4} {
		mode.Store(state)
		_, _, raw := f.request(t, "owner", "POST", "/api/v1/searchbots/ask", body)
		if strings.Contains(strings.ReplaceAll(raw, " ", ""), `"data":true`) || strings.Contains(raw, `"final":true`) || strings.Contains(raw, "private-provider-body") {
			t.Fatal("provider error/truncation fabricated Ask completion", raw)
		}
	}
	mode.Store(5)
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	raw, _ := json.Marshal(body)
	req, _ := http.NewRequestWithContext(ctx, "POST", f.url+"/api/v1/searchbots/ask", bytes.NewReader(raw))
	req.Header.Set("Authorization", "Bearer "+f.tokens["owner"])
	req.Header.Set("Content-Type", "application/json")
	response, err := f.client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	line, err := bufio.NewReader(response.Body).ReadString('\n')
	if err != nil || !strings.Contains(line, "grounded answer") {
		t.Fatal("Ask first frame did not cross write deadline", err, line)
	}
	cancel()
	response.Body.Close()
	select {
	case <-stopped:
	case <-time.After(2 * time.Second):
		t.Fatal("Ask caller cancellation did not release actual supplier")
	}
	if f.snapshot(t) != before {
		t.Fatal("Ask or refusal changed SQL")
	}
}
