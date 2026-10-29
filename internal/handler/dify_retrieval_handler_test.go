package handler

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/service/kg"
	"multirag/internal/service/nlp"
)

type difyTestDependencies struct {
	calls       []string
	kb          *entity.Knowledgebase
	readError   error
	existsErr   error
	metaError   error
	embedError  error
	chatError   error
	childError  error
	docError    error
	graphError  error
	enrichErr   error
	searchErr   error
	labelErr    error
	labelNil    bool
	metadata    common.MetaData
	chunks      []map[string]interface{}
	documents   []*entity.Document
	graph       *kg.Result
	request     *nlp.RetrievalRequest
	childrenFn  DifyChildrenRetriever
	metaFn      func(context.Context)
	authorizeFn func(context.Context)
}

func (d *difyTestDependencies) GetReadableByID(ctx context.Context, id, user string) (*entity.Knowledgebase, error) {
	d.calls = append(d.calls, "authorize:"+id+":"+user)
	if d.authorizeFn != nil {
		d.authorizeFn(ctx)
	}
	return d.kb, d.readError
}
func (d *difyTestDependencies) GetByIDContext(context.Context, string) (*entity.Knowledgebase, error) {
	d.calls = append(d.calls, "exists")
	return d.kb, d.existsErr
}
func (d *difyTestDependencies) Embedding(context.Context, *entity.Knowledgebase) (*models.EmbeddingModel, error) {
	d.calls = append(d.calls, "embedding")
	return &models.EmbeddingModel{}, d.embedError
}
func (d *difyTestDependencies) Chat(context.Context, *entity.Knowledgebase) (*models.ChatModel, error) {
	d.calls = append(d.calls, "chat")
	return &models.ChatModel{}, d.chatError
}
func (d *difyTestDependencies) GetFlattedMetaByKBsContext(ctx context.Context, ids []string) (common.MetaData, error) {
	d.calls = append(d.calls, "filter")
	if d.kb != nil && !reflect.DeepEqual(ids, []string{d.kb.ID}) {
		return nil, errors.New("unexpected metadata scope")
	}
	if d.metaFn != nil {
		d.metaFn(ctx)
	}
	return d.metadata, d.metaError
}
func (d *difyTestDependencies) EnrichChunksWithDocMetadata(_ context.Context, chunks []map[string]interface{}, user string, fields []string) ([]map[string]interface{}, error) {
	d.calls = append(d.calls, "enrich")
	if user != "user" || fields != nil {
		return nil, errors.New("unexpected enrichment scope")
	}
	result := make([]map[string]interface{}, len(chunks))
	for i, chunk := range chunks {
		copy := make(map[string]interface{}, len(chunk)+1)
		for key, value := range chunk {
			copy[key] = value
		}
		copy["document_metadata"] = map[string]interface{}{"count": 42, "enabled": true, "tags": []interface{}{"a", "b"}, "document_id": "untrusted", "doc_id": "untrusted"}
		result[i] = copy
	}
	return result, d.enrichErr
}
func (d *difyTestDependencies) LabelQuestionContext(_ context.Context, userID, question string, kbs []*entity.Knowledgebase) (map[string]float64, error) {
	d.calls = append(d.calls, "labels")
	if userID != "user" || question != "question" || len(kbs) != 1 || kbs[0].ID != "kb" {
		return nil, errors.New("unexpected ranking scope")
	}
	if d.labelNil {
		return nil, d.labelErr
	}
	return map[string]float64{"pagerank_fea": 10}, d.labelErr
}
func (d *difyTestDependencies) Retrieval(_ context.Context, request *nlp.RetrievalRequest) (*nlp.RetrievalResult, error) {
	d.calls = append(d.calls, "retrieve")
	d.request = request
	return &nlp.RetrievalResult{Chunks: d.chunks}, d.searchErr
}
func (d *difyTestDependencies) GetByIDs(_ context.Context, ids []string) ([]*entity.Document, error) {
	d.calls = append(d.calls, "documents")
	return d.documents, d.docError
}

type difyTestGraph struct{ deps *difyTestDependencies }

func (g difyTestGraph) Retrieval(_ context.Context, user string, ids []string, question string, _ *models.ChatModel, _ *models.EmbeddingModel, _ ...kg.Option) (*kg.Result, error) {
	g.deps.calls = append(g.deps.calls, "graph")
	if user != "user" || !reflect.DeepEqual(ids, []string{"kb"}) || question != "question" {
		return nil, errors.New("unexpected graph scope")
	}
	return g.deps.graph, g.deps.graphError
}

func newDifyTestHandler(t *testing.T, user *entity.User) (*difyTestDependencies, *gin.Engine) {
	t.Helper()
	d := &difyTestDependencies{
		kb:        &entity.Knowledgebase{ID: "kb", TenantID: "owner"},
		chunks:    []map[string]interface{}{{"doc_id": "doc", "kb_id": "kb", "similarity": 0.8, "content_with_weight": "ordinary", "docnm_kwd": "title", "vector": []float64{1}}},
		documents: []*entity.Document{{ID: "doc", KbID: "kb"}},
		graph:     &kg.Result{Content: "graph aggregate", KBID: "kb"},
	}
	children := func(ctx context.Context, chunks []map[string]interface{}, owner string) ([]map[string]interface{}, error) {
		d.calls = append(d.calls, "children")
		if owner != "owner" {
			return nil, errors.New("unexpected child owner")
		}
		if d.childrenFn != nil {
			return d.childrenFn(ctx, chunks, owner)
		}
		return chunks, d.childError
	}
	h := NewDifyRetrievalHandler(DifyRetrievalDependencies{Datasets: d, Models: d, Metadata: d, Retrieval: d, Documents: d, Children: children, KG: difyTestGraph{d}})
	gin.SetMode(gin.TestMode)
	router := gin.New()
	router.POST("/api/v1/dify/retrieval", func(c *gin.Context) {
		if user != nil {
			c.Set("user", user)
		}
		h.Retrieval(c)
	})
	router.GET("/api/v1/dify/retrieval/health", h.HealthCheck)
	return d, router
}

func callDify(router *gin.Engine, body string) *httptest.ResponseRecorder {
	response := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/v1/dify/retrieval", strings.NewReader(body))
	request.Header.Set("Content-Type", "application/json")
	router.ServeHTTP(response, request)
	return response
}

func TestDifyMetadataNoMatchStopsBeforeModelsAndRetrieval(t *testing.T) {
	d, router := newDifyTestHandler(t, &entity.User{ID: "user"})
	d.metadata = common.MetaData{"count": {"42": {"other"}}}
	response := callDify(router, `{"knowledge_id":"kb","query":"question","use_kg":true,"metadata_condition":{"conditions":[{"name":"count","comparison_operator":">","value":100}]}}`)
	if response.Code != 200 || strings.TrimSpace(response.Body.String()) != `{"records":[]}` {
		t.Fatal(response.Code, response.Body.String())
	}
	if !reflect.DeepEqual(d.calls, []string{"authorize:kb:user", "filter"}) {
		t.Fatal("zero match performed forbidden IO:", d.calls)
	}
}

func TestDifyTypedMetadataAndTrustedGraphIdentity(t *testing.T) {
	d, router := newDifyTestHandler(t, &entity.User{ID: "user"})
	d.metadata = common.MetaData{"count": {"42": {"doc"}}, "enabled": {"true": {"doc"}}, "tags": {"a": {"doc"}}}
	d.chunks = append(d.chunks,
		map[string]interface{}{"doc_id": "", "kb_id": "kb", "content_with_weight": "untrusted synthetic"},
		map[string]interface{}{"doc_id": "dangling", "kb_id": "kb", "content_with_weight": "deleted"},
		map[string]interface{}{"doc_id": "doc", "kb_id": "foreign", "content_with_weight": "foreign"},
		map[string]interface{}{"doc_id": "unselected", "kb_id": "kb", "content_with_weight": "unselected"})
	d.documents = append(d.documents, &entity.Document{ID: "unselected", KbID: "kb"})
	response := callDify(router, `{"knowledge_id":"kb","query":"question","use_kg":true,"retrieval_setting":{"score_threshold":0,"top_k":7},"metadata_condition":{"conditions":[{"name":"count","comparison_operator":"=","value":42},{"name":"enabled","comparison_operator":"=","value":true},{"name":"tags","comparison_operator":"in","value":["a","b"]}]}}`)
	if response.Code != http.StatusOK {
		t.Fatal(response.Code, response.Body.String())
	}
	var payload struct {
		Records []difyRecord `json:"records"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if len(payload.Records) != 2 || payload.Records[0].Content != "graph aggregate" || payload.Records[1].Content != "ordinary" {
		t.Fatal(payload.Records)
	}
	if !reflect.DeepEqual(payload.Records[0].Metadata, map[string]interface{}{"doc_id": "", "document_id": ""}) {
		t.Fatal(payload.Records[0])
	}
	metadata := payload.Records[1].Metadata
	if metadata["doc_id"] != "doc" || metadata["document_id"] != "doc" || metadata["enabled"] != true || metadata["count"] != float64(42) || !reflect.DeepEqual(metadata["tags"], []interface{}{"a", "b"}) {
		t.Fatal(metadata)
	}
	if d.request == nil || !reflect.DeepEqual(d.request.DocIDs, []string{"doc"}) || !reflect.DeepEqual(d.request.KbIDs, []string{"kb"}) || !reflect.DeepEqual(d.request.TenantIDs, []string{"owner"}) || d.request.Top == nil || *d.request.Top != 7 || d.request.SimilarityThreshold == nil || *d.request.SimilarityThreshold != 0 {
		t.Fatal(d.request)
	}
	if _, retained := d.chunks[0]["vector"]; !retained {
		t.Fatal("handler mutated caller's chunks")
	}
}

func TestDifyEmptyMetadataPredicatesDoNotSelectEmptyDocuments(t *testing.T) {
	for _, metadata := range []string{"", `,"metadata_condition":{}`, `,"metadata_condition":null`} {
		for _, useKG := range []string{"false", "true"} {
			t.Run(metadata+"/kg="+useKG, func(t *testing.T) {
				d, router := newDifyTestHandler(t, &entity.User{ID: "user"})
				response := callDify(router, `{"knowledge_id":"kb","query":"question","use_kg":`+useKG+metadata+`}`)
				if response.Code != 200 || d.request == nil || d.request.DocIDs != nil || *d.request.Top != 1024 || *d.request.SimilarityThreshold != 0 {
					t.Fatal(response.Code, response.Body.String(), d.request)
				}
				for _, call := range d.calls {
					if call == "filter" {
						t.Fatal("inactive predicates loaded filtering metadata")
					}
				}
			})
		}
	}
}

func TestDifyExplicitEmptyMetadataStopsAfterAuthorization(t *testing.T) {
	for _, useKG := range []string{"false", "true"} {
		t.Run(useKG, func(t *testing.T) {
			d, router := newDifyTestHandler(t, &entity.User{ID: "user"})
			response := callDify(router, `{"knowledge_id":"kb","query":"question","use_kg":`+useKG+`,"metadata_condition":{"conditions":[]}}`)
			if response.Code != http.StatusOK || response.Body.String() != `{"records":[]}` || !reflect.DeepEqual(d.calls, []string{"authorize:kb:user"}) {
				t.Fatal(response.Code, response.Body.String(), d.calls)
			}
		})
	}
}

func TestDifyExplicitEmptyMetadataKeepsAuthorizationChecks(t *testing.T) {
	const body = `{"knowledge_id":"kb","query":"question","use_kg":true,"metadata_condition":{"conditions":[]}}`
	t.Run("missing identity", func(t *testing.T) {
		d, router := newDifyTestHandler(t, nil)
		response := callDify(router, body)
		if response.Code != http.StatusForbidden || len(d.calls) != 0 {
			t.Fatal(response.Code, response.Body.String(), d.calls)
		}
	})
	t.Run("denied dataset", func(t *testing.T) {
		d, router := newDifyTestHandler(t, &entity.User{ID: "user"})
		d.readError = gorm.ErrRecordNotFound
		response := callDify(router, body)
		if response.Code != http.StatusForbidden || !reflect.DeepEqual(d.calls, []string{"authorize:kb:user", "exists"}) {
			t.Fatal(response.Code, response.Body.String(), d.calls)
		}
	})
	t.Run("original token tenant", func(t *testing.T) {
		d := &difyTestDependencies{kb: &entity.Knowledgebase{ID: "kb", TenantID: "foreign-owner"}}
		h := NewDifyRetrievalHandler(DifyRetrievalDependencies{Datasets: d, Metadata: d})
		router := gin.New()
		router.POST("/api/v1/dify/retrieval", func(c *gin.Context) {
			c.Set("user", &entity.User{ID: "user"})
			c.Set(difyTokenTenantContext, "original-token-tenant")
			h.Retrieval(c)
		})
		response := callDify(router, body)
		if response.Code != http.StatusForbidden || !reflect.DeepEqual(d.calls, []string{"authorize:kb:user"}) {
			t.Fatal(response.Code, response.Body.String(), d.calls)
		}
	})
}

func TestDifyNullMetadataConditionsRejectBeforeIO(t *testing.T) {
	for _, useKG := range []string{"false", "true"} {
		t.Run(useKG, func(t *testing.T) {
			d, router := newDifyTestHandler(t, &entity.User{ID: "user"})
			response := callDify(router, `{"knowledge_id":"kb","query":"question","use_kg":`+useKG+`,"metadata_condition":{"conditions":null}}`)
			if response.Code != http.StatusBadRequest || !strings.Contains(response.Body.String(), `"error_code":101`) || len(d.calls) != 0 {
				t.Fatal(response.Code, response.Body.String(), d.calls)
			}
		})
	}
}

func TestDifyExplicitEmptyMetadataCancellation(t *testing.T) {
	d, router := newDifyTestHandler(t, &entity.User{ID: "user"})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	d.authorizeFn = func(context.Context) { cancel() }
	response := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/v1/dify/retrieval", strings.NewReader(`{"knowledge_id":"kb","query":"question","use_kg":true,"metadata_condition":{"conditions":[]}}`)).WithContext(ctx)
	router.ServeHTTP(response, request)
	if response.Code != http.StatusInternalServerError || !reflect.DeepEqual(d.calls, []string{"authorize:kb:user"}) {
		t.Fatal(response.Code, response.Body.String(), d.calls)
	}
}

func TestDifyAbsentRankFeaturesPreserveRetrieverDefaults(t *testing.T) {
	d, router := newDifyTestHandler(t, &entity.User{ID: "user"})
	d.labelNil = true
	response := callDify(router, `{"knowledge_id":"kb","query":"question"}`)
	if response.Code != 200 || d.request == nil || d.request.RankFeature != nil {
		t.Fatal(response.Code, response.Body.String(), d.request)
	}
}

func TestDifyRejectsInvalidArgumentsBeforeDependencyIO(t *testing.T) {
	for _, body := range []string{
		`{}`, `null`, `[]`, `{bad`, `{"knowledge_id":"kb","query":"question"}{}`,
		`{"knowledge_id":"kb","query":" "}`,
		`{"knowledge_id":"kb","query":"question","use_kg":null}`,
		`{"knowledge_id":"kb","query":"question","retrieval_setting":null}`,
		`{"knowledge_id":"kb","query":"question","retrieval_setting":{"top_k":0}}`,
		`{"knowledge_id":"kb","query":"question","retrieval_setting":{"top_k":10001}}`,
		`{"knowledge_id":"kb","query":"question","retrieval_setting":{"top_k":1.2}}`,
		`{"knowledge_id":"kb","query":"question","retrieval_setting":{"top_k":null}}`,
		`{"knowledge_id":"kb","query":"question","retrieval_setting":{"score_threshold":null}}`,
		`{"knowledge_id":"kb","query":"question","retrieval_setting":{"score_threshold":1e999}}`,
		`{"knowledge_id":"kb","query":"question","retrieval_setting":{"score_threshold":-0.1}}`,
		`{"knowledge_id":"kb","query":"question","metadata_condition":{"conditions":null}}`,
		`{"knowledge_id":"kb","query":"question","metadata_condition":{"conditions":[{"name":"author","comparison_operator":"in","value":{}}]}}`,
		`{"knowledge_id":"kb","query":"question","metadata_condition":{"logic":"xor","conditions":[]}}`,
		`{"knowledge_id":"kb","query":"question","metadata_condition":{"logic":"","conditions":[]}}`,
		`{"knowledge_id":"kb","query":"question","search_mode":{"type":"dense"}}`,
	} {
		t.Run(body, func(t *testing.T) {
			d, router := newDifyTestHandler(t, &entity.User{ID: "user"})
			response := callDify(router, body)
			if response.Code != 400 || !strings.Contains(response.Body.String(), `"error_code":101`) || len(d.calls) != 0 {
				t.Fatal(response.Code, response.Body.String(), d.calls)
			}
		})
	}
}

func TestDifyCSVStringMembership(t *testing.T) {
	d, router := newDifyTestHandler(t, &entity.User{ID: "user"})
	d.metadata = common.MetaData{"author": {"Alice": {"doc"}}}
	response := callDify(router, `{"knowledge_id":"kb","query":"question","metadata_condition":{"conditions":[{"name":"author","comparison_operator":"in","value":" Alice, Bob "}]}}`)
	if response.Code != http.StatusOK || d.request == nil || !reflect.DeepEqual(d.request.DocIDs, []string{"doc"}) || !strings.Contains(response.Body.String(), `"content":"ordinary"`) {
		t.Fatal(response.Code, response.Body.String(), d.request)
	}
}

func TestDifyAuthorizationPrecedesMetadata(t *testing.T) {
	for _, user := range []*entity.User{nil, {ID: ""}, {ID: " "}} {
		d, router := newDifyTestHandler(t, user)
		response := callDify(router, `{"knowledge_id":"kb","query":"question","use_kg":true}`)
		if response.Code != 403 || len(d.calls) != 0 {
			t.Fatal(response.Code, response.Body.String(), d.calls)
		}
	}
	for _, missing := range []bool{false, true} {
		d, router := newDifyTestHandler(t, &entity.User{ID: "user"})
		d.readError = gorm.ErrRecordNotFound
		status := 403
		if missing {
			d.existsErr, status = gorm.ErrRecordNotFound, 404
		}
		response := callDify(router, `{"knowledge_id":"kb","query":"question","metadata_condition":{"conditions":[{"name":"x","comparison_operator":"=","value":1}]}}`)
		if response.Code != status || !reflect.DeepEqual(d.calls, []string{"authorize:kb:user", "exists"}) {
			t.Fatal(response.Code, response.Body.String(), d.calls)
		}
	}
}

func TestDifyAPITokenRetainsOriginalTenantScope(t *testing.T) {
	d := &difyTestDependencies{kb: &entity.Knowledgebase{ID: "kb", TenantID: "foreign-owner"}}
	h := NewDifyRetrievalHandler(DifyRetrievalDependencies{Datasets: d, Metadata: d})
	router := gin.New()
	router.POST("/api/v1/dify/retrieval", func(c *gin.Context) {
		c.Set("user", &entity.User{ID: "user"})
		c.Set(difyTokenTenantContext, "original-token-tenant")
		h.Retrieval(c)
	})
	response := callDify(router, `{"knowledge_id":"kb","query":"question","use_kg":true,"metadata_condition":{"conditions":[{"name":"x","comparison_operator":"=","value":1}]}}`)
	if response.Code != 403 || !reflect.DeepEqual(d.calls, []string{"authorize:kb:user"}) {
		t.Fatal(response.Code, response.Body.String(), d.calls)
	}
}

func TestDifyGraphCannotReturnAnotherDatasetSummary(t *testing.T) {
	d, router := newDifyTestHandler(t, &entity.User{ID: "user"})
	d.graph.KBID = "foreign"
	response := callDify(router, `{"knowledge_id":"kb","query":"question","use_kg":true}`)
	if response.Code != 500 || strings.Contains(response.Body.String(), "graph aggregate") {
		t.Fatal(response.Code, response.Body.String())
	}
}

func TestDifyPropagatesResourceFailures(t *testing.T) {
	for _, boundary := range []string{"dataset", "metadata", "embedding", "labels", "retrieval", "children", "documents", "enrich", "chat", "graph"} {
		t.Run(boundary, func(t *testing.T) {
			d, router := newDifyTestHandler(t, &entity.User{ID: "user"})
			failure := errors.New("private connection credential must not appear in response")
			metadata := ""
			switch boundary {
			case "dataset":
				d.readError = failure
			case "metadata":
				d.metaError = failure
				metadata = `,"metadata_condition":{"conditions":[{"name":"x","comparison_operator":"=","value":1}]}`
			case "embedding":
				d.embedError = failure
			case "labels":
				d.labelErr = failure
			case "retrieval":
				d.searchErr = failure
			case "children":
				d.childError = failure
			case "documents":
				d.docError = failure
			case "enrich":
				d.enrichErr = failure
			case "chat":
				d.chatError = failure
			case "graph":
				d.graphError = failure
			}
			response := callDify(router, `{"knowledge_id":"kb","query":"question","use_kg":true`+metadata+`}`)
			if response.Code != 500 || !strings.Contains(response.Body.String(), `"error_code":500`) || strings.Contains(response.Body.String(), "credential") {
				t.Fatal(response.Code, response.Body.String(), d.calls)
			}
		})
	}
}

func TestDifyParentExpansionCannotBroadenSelection(t *testing.T) {
	d, router := newDifyTestHandler(t, &entity.User{ID: "user"})
	d.metadata = common.MetaData{"author": {"Alice": {"doc"}}}
	d.documents = append(d.documents, &entity.Document{ID: "other", KbID: "kb"})
	d.childrenFn = func(_ context.Context, chunks []map[string]interface{}, _ string) ([]map[string]interface{}, error) {
		return append(chunks, map[string]interface{}{"kb_id": "kb", "doc_id": "other", "content_with_weight": "wrong document"}), nil
	}
	response := callDify(router, `{"knowledge_id":"kb","query":"question","metadata_condition":{"conditions":[{"name":"author","comparison_operator":"=","value":"Alice"}]}}`)
	if response.Code != 200 || strings.Contains(response.Body.String(), "wrong document") {
		t.Fatal(response.Code, response.Body.String())
	}
}

func TestDifyCancellationDoesNotReportEmptySuccess(t *testing.T) {
	d, router := newDifyTestHandler(t, &entity.User{ID: "user"})
	ctx, cancel := context.WithCancel(context.Background())
	d.metaFn = func(context.Context) { cancel() }
	response := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/v1/dify/retrieval", strings.NewReader(`{"knowledge_id":"kb","query":"question","use_kg":true,"metadata_condition":{"conditions":[{"name":"x","comparison_operator":"=","value":1}]}}`)).WithContext(ctx)
	router.ServeHTTP(response, request)
	if response.Code != 500 || !reflect.DeepEqual(d.calls, []string{"authorize:kb:user", "filter"}) {
		t.Fatal(response.Code, response.Body.String(), d.calls)
	}
}

func TestDifyHealthHasNoRetrievalDependencies(t *testing.T) {
	handler := NewDifyRetrievalHandler(DifyRetrievalDependencies{})
	router := gin.New()
	router.GET("/health", handler.HealthCheck)
	response := httptest.NewRecorder()
	router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/health?knowledge_id=kb&query=ignored", nil))
	if response.Code != 200 || response.Body.String() != `{"code":0,"data":true}` {
		t.Fatal(response.Code, response.Body.String())
	}
}

func TestDifyNonFiniteChunkScoreIsFailure(t *testing.T) {
	for _, value := range []interface{}{math.Inf(1), math.NaN(), "0.5"} {
		d, router := newDifyTestHandler(t, &entity.User{ID: "user"})
		d.chunks[0]["similarity"] = value
		response := callDify(router, `{"knowledge_id":"kb","query":"question"}`)
		if response.Code != 500 {
			t.Fatal(response.Code, response.Body.String())
		}
	}
}
