package kg

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"reflect"
	"sync/atomic"
	"testing"
	"time"

	"multirag/internal/entity/models"
	"multirag/internal/testsupport"
)

// Observe the request-scoped binding while delegating to the real HTTP driver.
// OpenAI embeddings have no query/document task field in their wire protocol.
type kgObservedHTTPEmbedding struct {
	models.ModelDriver
	query     bool
	dimension int
	context   context.Context
}

func (d *kgObservedHTTPEmbedding) Embed(name *string, texts []string, api *models.APIConfig, cfg *models.EmbeddingConfig) ([]models.EmbeddingData, error) {
	d.query, d.dimension, d.context = cfg.Query, cfg.Dimension, api.Context
	return d.ModelDriver.Embed(name, texts, api, cfg)
}

func kgRecordEmbeddingInput(t *testing.T, journal *testsupport.CaseJournal, endpoint, modelName, question string, dimension int) {
	t.Helper()
	raw, err := json.Marshal(struct {
		Endpoint, Model, Question string
		Dimension                 int
		Query                     bool
	}{endpoint, modelName, question, dimension, true})
	if err != nil {
		t.Fatal(err)
	}
	hash := sha256.Sum256(raw)
	journal.Event("embedding-input", map[string]any{"sha256": hex.EncodeToString(hash[:]), "endpoint": endpoint, "dimension": dimension, "query": true})
}

func TestKGEmbeddingRealOpenAIHTTPBinding(t *testing.T) {
	journal := testsupport.NewCase(t)
	var calls atomic.Int64
	fixture := testsupport.RegisteredHTTPServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Method != http.MethodPost || r.URL.Path != "/v1/embeddings" || r.Header.Get("Authorization") != "Bearer owned-kg-fixture-key" {
			t.Error("embedding request route or fixture credential changed")
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		if body["model"] != "owned-embed" || body["dimensions"] != float64(2) || !reflect.DeepEqual(body["input"], []any{"owned KG question"}) {
			t.Error("embedding input or dimension changed", body)
		}
		if _, exists := body["query"]; exists {
			t.Error("OpenAI request invented a query task parameter")
		}
		journal.Event("embedding-request-readback", map[string]any{"path": r.URL.Path, "body": body})
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"data":[{"index":0,"embedding":[1,0]}]}`)
	}))
	name, key, region := "owned-embed", "owned-kg-fixture-key", "owned"
	api := &models.APIConfig{Context: context.Background(), APIKey: &key, Region: &region}
	config := &models.EmbeddingConfig{Dimension: 2, Query: false}
	originalAPI, originalConfig := *api, *config
	driver := &kgObservedHTTPEmbedding{ModelDriver: models.NewOpenAIModel(map[string]string{region: fixture.URL + "/v1"}, models.URLSuffix{Embedding: "embeddings"})}
	bound := models.NewEmbeddingModel(driver, &name, api)
	bound.EmbeddingConfig = config
	kgRecordEmbeddingInput(t, journal, fixture.URL+"/v1/embeddings", name, "owned KG question", 2)
	dense, err := buildKGDenseExpr(t.Context(), bound, "owned KG question", 3)
	if err != nil || dense == nil {
		t.Fatal(dense, err)
	}
	if dense.VectorColumnName != "q_2_vec" || !reflect.DeepEqual(dense.EmbeddingData, []float64{1, 0}) || dense.TopN != 3 {
		t.Fatal("bound HTTP vector contract lost", dense)
	}
	if !driver.query || driver.dimension != 2 || driver.context != t.Context() || calls.Load() != 1 {
		t.Fatal("request query context or dimensions lost", driver.query, driver.dimension, calls.Load())
	}
	if bound.APIConfig != api || bound.EmbeddingConfig != config || !reflect.DeepEqual(*api, originalAPI) || *config != originalConfig {
		t.Fatal("shared API or embedding binding mutated")
	}
	journal.Event("embedding-result-readback", map[string]any{"requests": calls.Load(), "vector_column": dense.VectorColumnName, "vector": dense.EmbeddingData, "binding_unchanged": true})
}

func TestKGEmbeddingInFlightCancellationDrainsHTTP(t *testing.T) {
	journal := testsupport.NewCase(t)
	entered, handlerDone, callDone := make(chan struct{}), make(chan struct{}), make(chan struct{})
	var requests atomic.Int64
	fixture := testsupport.RegisteredHTTPServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		defer close(handlerDone)
		if _, err := io.Copy(io.Discard, r.Body); err != nil {
			t.Error("failed to drain request body", err)
			return
		}
		close(entered)
		timer := time.NewTimer(2 * time.Second)
		defer timer.Stop()
		select {
		case <-r.Context().Done():
			journal.Event("embedding-handler-cancelled", map[string]any{"body_drained": true})
		case <-timer.C:
			t.Error("embedding handler did not receive cancellation within 2s")
			w.WriteHeader(http.StatusGatewayTimeout)
		}
	}))
	name, key := "owned-embed", "owned-kg-fixture-key"
	api := &models.APIConfig{APIKey: &key}
	config := &models.EmbeddingConfig{Dimension: 2}
	originalAPI, originalConfig := *api, *config
	bound := models.NewEmbeddingModel(models.NewOpenAIModel(map[string]string{"default": fixture.URL + "/v1"}, models.URLSuffix{Embedding: "embeddings"}), &name, api)
	bound.EmbeddingConfig = config
	store := &kgStore{}
	svc := &KGSearchService{store: store, kbReader: &kgReader{}}
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	var returnedErr error
	t.Cleanup(func() {
		cancel()
		select {
		case <-callDone:
		case <-time.After(2 * time.Second):
			t.Error("KG search goroutine survived cancellation")
		}
		select {
		case <-handlerDone:
		case <-time.After(2 * time.Second):
			t.Error("HTTP handler goroutine survived cancellation")
		}
	})
	kgRecordEmbeddingInput(t, journal, fixture.URL+"/v1/embeddings", name, "owned KG question", 2)
	go func() {
		defer close(callDone)
		_, returnedErr = svc.SearchKGEntities(ctx, "owner", []string{"kb-a"}, "owned KG question", bound, 3)
	}()
	select {
	case <-entered:
		cancel()
	case <-time.After(2 * time.Second):
		t.Fatal("embedding HTTP request did not enter its handler")
	}
	select {
	case <-callDone:
	case <-time.After(2 * time.Second):
		t.Fatal("KG search did not return after in-flight cancellation")
	}
	select {
	case <-handlerDone:
	case <-time.After(2 * time.Second):
		t.Fatal("embedding HTTP handler did not drain after cancellation")
	}
	if !errors.Is(returnedErr, context.Canceled) || requests.Load() != 1 || store.calls != 0 {
		t.Fatal("cancelled embedding reached engine or lost context error", returnedErr, requests.Load(), store.calls)
	}
	if bound.APIConfig != api || bound.EmbeddingConfig != config || !reflect.DeepEqual(*api, originalAPI) || *config != originalConfig {
		t.Fatal("cancelled call mutated shared embedding configuration")
	}
	journal.Event("embedding-cancellation-readback", map[string]any{"requests": requests.Load(), "engine_calls": store.calls, "call_joined": true, "handler_joined": true, "binding_unchanged": true})
}
