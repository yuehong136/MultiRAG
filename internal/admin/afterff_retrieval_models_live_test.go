package admin

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/service"
	"multirag/internal/service/nlp"
	"multirag/internal/testsupport"
)

type afterFFDefaultRetrievalEngine struct {
	engine.DocEngine
	testing         *testing.T
	tenant, dataset string
	calls           int
}

func (e *afterFFDefaultRetrievalEngine) Search(_ context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	e.calls++
	if len(req.IndexNames) != 1 || req.IndexNames[0] != "multirag_"+e.tenant || len(req.KbIDs) != 1 || req.KbIDs[0] != e.dataset {
		e.testing.Error("retrieval lost authorized index/dataset scope")
	}
	vectorFound := false
	for _, expr := range req.MatchExprs {
		if dense, ok := expr.(*types.MatchDenseExpr); ok {
			vectorFound = len(dense.EmbeddingData) == 3 && dense.EmbeddingData[0] == 0.25
		}
	}
	if !vectorFound {
		e.testing.Error("actual provider embedding did not reach dense search")
	}
	return &types.SearchResult{Chunks: []map[string]interface{}{}, Total: 0}, nil
}
func (*afterFFDefaultRetrievalEngine) GetDocIDs([]map[string]interface{}) []string { return nil }
func (*afterFFDefaultRetrievalEngine) GetFields([]map[string]interface{}, []string) map[string]map[string]interface{} {
	return nil
}
func (*afterFFDefaultRetrievalEngine) GetAggregation([]map[string]interface{}, string) []map[string]interface{} {
	return nil
}
func (*afterFFDefaultRetrievalEngine) GetHighlight([]map[string]interface{}, []string, string) map[string]string {
	return nil
}

func afterFFCheckDefaultRetrieval(t *testing.T, j *testsupport.CaseJournal, reader *sql.DB, tenantID, foreignTenantID, kbID string, transport http.RoundTripper) {
	t.Helper()
	var selector string
	var numericID, foreignID, chatID int64
	if err := reader.QueryRow("SELECT embd_id, tenant_embd_id, tenant_llm_id FROM t_ai_tenants WHERE id=$1", tenantID).Scan(&selector, &numericID, &chatID); err != nil {
		t.Fatal("default retrieval tenant readback failed")
	}
	if err := reader.QueryRow("SELECT tenant_embd_id FROM t_ai_tenants WHERE id=$1", foreignTenantID).Scan(&foreignID); err != nil {
		t.Fatal("foreign retrieval binding readback failed")
	}
	parts := strings.Split(selector, "@")
	if len(parts) != 3 {
		t.Fatal("canonical configured embedding selector required")
	}
	var instanceID, originalExtra, originalName string
	if err := reader.QueryRow("SELECT i.id, i.extra FROM tenant_model_instance i JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.tenant_id=$1 AND p.provider_name=$2 AND i.instance_name=$3", tenantID, parts[2], parts[1]).Scan(&instanceID, &originalExtra); err != nil {
		t.Fatal("configured embedding instance missing")
	}
	if err := reader.QueryRow("SELECT llm_name FROM t_ai_tenant_llms WHERE id=$1 AND tenant_id=$2", numericID, tenantID).Scan(&originalName); err != nil {
		t.Fatal("numeric embedding binding missing")
	}
	previousTransport := http.DefaultTransport
	http.DefaultTransport = transport
	defer func() { http.DefaultTransport = previousTransport }()
	address := "127.0.0.1:43853"
	j.Event("reserve-retrieval-provider-listener-before-bind", map[string]any{"address": address, "tenant_id": tenantID, "kb_id": kbID})
	listener, err := net.Listen("tcp", address)
	if err != nil {
		t.Fatal("owned retrieval provider listener unavailable")
	}
	var calls atomic.Int64
	probe := &http.Server{ReadHeaderTimeout: time.Second, Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var body struct {
			Model string   `json:"model"`
			Input []string `json:"input"`
		}
		if r.Method != http.MethodPost || r.URL.Path != "/v1/embeddings" || json.NewDecoder(r.Body).Decode(&body) != nil || body.Model != parts[0] || len(body.Input) != 1 || body.Input[0] != "hello" {
			t.Error("unexpected controlled embedding request")
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"data":[{"index":0,"embedding":[0.25,0.5,0.75]}]}`)
	})}
	done := make(chan error, 1)
	go func() { done <- probe.Serve(listener) }()
	defer func() {
		_ = probe.Close()
		<-done
		j.Event("closed-retrieval-provider-listener", map[string]any{"address": address})
	}()
	extra := `{"region":"default","base_url":"http://` + address + `/v1"}`
	j.Event("reserve-retrieval-fixture-updates-before-SQL", map[string]any{"instance_id": instanceID, "numeric_id": numericID, "kb_id": kbID})
	defer func() {
		if _, err := reader.Exec("UPDATE tenant_model_instance SET extra=$1 WHERE id=$2", originalExtra, instanceID); err != nil {
			t.Error("restore retrieval instance failed")
		}
		if _, err := reader.Exec("UPDATE t_ai_tenant_llms SET llm_name=$1,status='1' WHERE id=$2", originalName, numericID); err != nil {
			t.Error("restore retrieval binding failed")
		}
		if _, err := reader.Exec("UPDATE t_ai_knowledgebases SET embd_id=$1,tenant_embd_id=NULL WHERE id=$2", selector, kbID); err != nil {
			t.Error("restore retrieval dataset failed")
		}
	}()
	if _, err := reader.Exec("UPDATE tenant_model_instance SET extra=$1 WHERE id=$2", extra, instanceID); err != nil {
		t.Fatal("configure controlled embedding endpoint failed")
	}
	// A canonical modern selector must work even when no legacy name matches it.
	if _, err := reader.Exec("UPDATE t_ai_tenant_llms SET llm_name=$1 WHERE id=$2", originalName+"-owned-unmatched", numericID); err != nil {
		t.Fatal("isolate modern selector failed")
	}
	if err := nlp.InitQueryBuilder(""); err != nil {
		t.Fatal(err)
	}
	store := &afterFFDefaultRetrievalEngine{testing: t, tenant: tenantID, dataset: kbID}
	chunk := service.NewChunkService(store)
	retrieve := func(wantError bool) {
		t.Helper()
		beforeHTTP, beforeIndex := calls.Load(), store.calls
		result, err := chunk.RetrievalTest(context.Background(), &service.RetrievalTestRequest{DatasetIDs: []string{kbID}, Question: "hello"}, tenantID)
		if wantError {
			if err == nil || result != nil || calls.Load() != beforeHTTP || store.calls != beforeIndex {
				t.Fatal("invalid numeric retrieval binding had model/index effects")
			}
		} else if err != nil || result == nil || calls.Load() != beforeHTTP+1 || store.calls <= beforeIndex {
			t.Fatalf("default retrieval did not reach controlled model and index: %v", err)
		}
	}
	retrieve(false)
	if _, err := reader.Exec("UPDATE t_ai_knowledgebases SET embd_id='' WHERE id=$1", kbID); err != nil {
		t.Fatal("configure tenant default retrieval failed")
	}
	retrieve(false)
	if _, err := reader.Exec("UPDATE t_ai_tenant_llms SET llm_name=$1 WHERE id=$2", originalName, numericID); err != nil {
		t.Fatal("restore numeric retrieval name failed")
	}
	for _, id := range []int64{foreignID, chatID, 0} {
		if _, err := reader.Exec("UPDATE t_ai_knowledgebases SET tenant_embd_id=$1 WHERE id=$2", id, kbID); err != nil {
			t.Fatal("configure rejected retrieval reference failed")
		}
		retrieve(true)
	}
	if _, err := reader.Exec("UPDATE t_ai_knowledgebases SET tenant_embd_id=$1 WHERE id=$2", numericID, kbID); err != nil {
		t.Fatal("configure numeric retrieval failed")
	}
	retrieve(false)
	if _, err := reader.Exec("UPDATE t_ai_tenant_llms SET status='0' WHERE id=$1", numericID); err != nil {
		t.Fatal("configure inactive retrieval binding failed")
	}
	retrieve(true)
	j.Event("default-retrieval-consumer-readback", map[string]any{"tenant_id": tenantID, "kb_id": kbID, "provider_calls": calls.Load(), "index_calls": store.calls, "canonical_without_legacy_name": true, "tenant_default": true, "owned_numeric": true, "foreign_wrong_type_zero_inactive_no_effects": true})
}
