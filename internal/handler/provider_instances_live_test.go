package handler_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/go-sql-driver/mysql"
	"io"
	"multirag/internal/cli"
	"multirag/internal/server/local"
	"multirag/internal/testsupport"
	"net/http"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestProviderInstancesLiveHTTPAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	var calls [2]atomic.Int64
	var received [2]atomic.Value
	ids := make([]string, 2)
	for i := range ids {
		up := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls[i].Add(1)
			if r.Method != "POST" || r.URL.Path != "/chat/completions" {
				t.Error("original vllm endpoint changed")
			}
			if r.Header.Get("Authorization") != fmt.Sprintf("Bearer instance-key-%d", i) {
				t.Error("instance credential crossed")
			}
			var body map[string]any
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Error(err)
			}
			received[i].Store(body["messages"])
			if body["model"] != "fixture-model" {
				t.Error("model name changed")
			}
			if body["stream"] == true {
				fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer-%d\"}}]}\n\ndata: [DONE]\n\n", i)
			} else {
				fmt.Fprintf(w, `{"choices":[{"message":{"content":"answer-%d"}}]}`, i)
			}
		}))
		ids[i] = f.enroll(t, "vllm", fmt.Sprintf("instance-%d", i), fmt.Sprintf("instance-key-%d", i), up.URL, "fixture-model", "chat")
	}
	history := []any{map[string]any{"role": "system", "content": "rules"}, map[string]any{"role": "assistant", "content": "previous"}, map[string]any{"role": "user", "content": []any{map[string]any{"type": "text", "text": "question"}}}}
	for i, id := range ids {
		result := f.requireSuccess(t, "owner", "POST", "/api/v1/chat/completions", map[string]any{"model_id": id, "messages": history})
		if result["answer"] != fmt.Sprintf("answer-%d", i) {
			t.Fatal("actual inference answer lost")
		}
		if !reflect.DeepEqual(received[i].Load(), history) {
			t.Fatal("history never reached supplier")
		}
		_, _, raw := f.request(t, "owner", "POST", "/api/v1/chat/completions", map[string]any{"model_id": id, "messages": history, "stream": true})
		if !strings.Contains(raw, "[DONE]") || !strings.Contains(raw, fmt.Sprintf("answer-%d", i)) {
			t.Fatal("real stream absent")
		}
	}
	before := f.counts(t)
	beforeSnapshot := f.snapshot(t)
	supplierCalls := calls[0].Load() + calls[1].Load()
	for _, actor := range []string{"", "foreign", "inactive"} {
		_, result, _ := f.request(t, actor, "POST", "/api/v1/chat/completions", map[string]any{"model_id": ids[0], "messages": history})
		if providerEnvelopeError(result) == nil {
			t.Fatal("foreign/disabled actor inferred")
		}
	}
	if f.snapshot(t) != beforeSnapshot || f.counts(t) != before || calls[0].Load()+calls[1].Load() != supplierCalls {
		t.Fatal("denied inference had side effects")
	}

	// These actual auth branches must stop the protected chain before SQL or supplier calls.
	status, result, _ := f.request(t, "superuser", "PUT", "/api/v1/providers/", map[string]any{"provider_name": "OpenAI"})
	if status != 403 || providerEnvelopeError(result) == nil || f.snapshot(t) != beforeSnapshot || f.counts(t) != before {
		t.Fatal("superuser denial continued protected mutation")
	}
	local.SetAdminStatus(1, "controlled unavailable runtime")
	status, result, _ = f.request(t, "owner", "PUT", "/api/v1/providers/", map[string]any{"provider_name": "OpenAI"})
	if status != 503 || providerEnvelopeError(result) == nil || f.snapshot(t) != beforeSnapshot || f.counts(t) != before {
		t.Fatal("license denial continued protected mutation")
	}
	status, result, _ = f.request(t, "owner", "POST", "/api/v1/chat/completions", map[string]any{"model_id": ids[0], "messages": history})
	if status != 503 || providerEnvelopeError(result) == nil || calls[0].Load()+calls[1].Load() != supplierCalls {
		t.Fatal("license denial reached supplier")
	}
	local.SetAdminStatus(0, "controlled owned provider fixture")
	// Disabled registered rows must not be used by either selector route.
	path := "/api/v1/providers/vllm/instances/instance-0/models/fixture-model"
	f.requireSuccess(t, "owner", "PATCH", path, map[string]any{"status": "inactive"})
	_, result, _ = f.request(t, "owner", "POST", "/api/v1/chat/completions", map[string]any{"model_id": ids[0], "messages": history})
	if providerEnvelopeError(result) == nil {
		t.Fatal("inactive model inferred")
	}
	if calls[0].Load()+calls[1].Load() != supplierCalls {
		t.Fatal("inactive model contacted supplier")
	}
	f.requireSuccess(t, "owner", "PATCH", path, map[string]any{"status": "active"})
	var metadata, modelStatus string
	if err := f.Read.QueryRow("SELECT extra,status FROM tenant_model WHERE id=?", ids[0]).Scan(&metadata, &modelStatus); err != nil || modelStatus != "active" {
		t.Fatal("model status readback", err)
	}
	var extra map[string]any
	if json.Unmarshal([]byte(metadata), &extra) != nil || extra["max_tokens"] != float64(128) {
		t.Fatal("canonical declaration metadata lost")
	}
	// Actual target Models[] declaration locks the existing instance for one concurrent winner.
	var workers sync.WaitGroup
	results := make(chan bool, 6)
	for range 6 {
		workers.Go(func() {
			_, result, _ := f.request(t, "owner", "POST", "/api/v1/providers/vllm/instances/instance-0/models", map[string]any{"models": []any{map[string]any{"model_name": "concurrent", "model_types": []string{"chat"}}}})
			results <- providerEnvelopeError(result) == nil
		})
	}
	workers.Wait()
	close(results)
	successes := 0
	for ok := range results {
		if ok {
			successes++
		}
	}
	if successes != 1 {
		t.Fatal("concurrent declaration winners", successes)
	}
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM tenant_model WHERE model_name=?", "concurrent").Scan(&count); err != nil || count != 1 {
		t.Fatal("concurrent physical rows", count, err)
	}
	results = make(chan bool, 6)
	for range 6 {
		workers.Go(func() {
			_, result, _ := f.request(t, "owner", "POST", "/api/v1/providers/vllm/instances", map[string]any{"instance_name": "concurrent-instance", "api_key": "instance-fixture", "base_url": "http://127.0.0.1:1"})
			results <- providerEnvelopeError(result) == nil
		})
	}
	workers.Wait()
	close(results)
	successes = 0
	for ok := range results {
		if ok {
			successes++
		}
	}
	if err := f.Read.QueryRow("SELECT count(*) FROM tenant_model_instance WHERE instance_name=?", "concurrent-instance").Scan(&count); err != nil || successes != 1 || count != 1 {
		t.Fatal("concurrent instance creation duplicated or lost SQL rows", successes, count, err)
	}
}

func TestRoot55BindingSQLFailureAndCancellation(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	var calls atomic.Int64
	var blocking atomic.Bool
	entered, stopped := make(chan struct{}, 1), make(chan struct{}, 1)
	supplier := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("Authorization") != "Bearer stored-silicon-key" || r.URL.Path != "/embeddings" {
			t.Error("bound supplier identity changed")
		}
		if blocking.Load() {
			w.WriteHeader(200)
			w.(http.Flusher).Flush()
			entered <- struct{}{}
			select {
			case <-r.Context().Done():
				stopped <- struct{}{}
			case <-time.After(5 * time.Second):
			}
			return
		}
		fmt.Fprint(w, `{"data":[{"index":0,"embedding":[0,1]}]}`)
	}))
	id := f.enroll(t, "SiliconFlow", "sql-fault", "stored-silicon-key", supplier.URL, "BAAI/bge-m3", "embedding")
	before := f.snapshot(t)
	f.requireSuccess(t, "owner", "POST", "/api/v1/embeddings", map[string]any{"model_id": id, "texts": []string{"q"}})
	count := calls.Load()
	f.Journal.Event("reserve-owned-table-rename", map[string]any{"table": "tenant_model", "temporary": "root55_hidden_model"})
	if _, err := f.Read.Exec("RENAME TABLE tenant_model TO root55_hidden_model"); err != nil {
		t.Fatal(err)
	}
	restored := false
	t.Cleanup(func() {
		if !restored {
			if _, err := f.Read.Exec("RENAME TABLE root55_hidden_model TO tenant_model"); err != nil {
				t.Error(err)
			}
		}
	})
	_, err := f.svc.GetEmbeddingModel("owner-tenant", "BAAI/bge-m3@sql-fault@SiliconFlow")
	var sqlError *mysql.MySQLError
	if err == nil || !errors.As(err, &sqlError) || sqlError.Number != 1146 {
		t.Fatal("physical SQL failure was hidden as missing/disabled model")
	}
	_, failed, _ := f.request(t, "owner", "POST", "/api/v1/embeddings", map[string]any{"model_id": id, "texts": []string{"q"}})
	if providerEnvelopeError(failed) == nil || calls.Load() != count {
		t.Fatal("SQL failure fell back to supplier")
	}
	if _, err := f.Read.Exec("RENAME TABLE root55_hidden_model TO tenant_model"); err != nil {
		t.Fatal(err)
	}
	restored = true
	if f.snapshot(t) != before {
		t.Fatal("SQL fault changed committed rows")
	}
	f.Journal.Event("owned-table-restored-readback", map[string]any{"table": "tenant_model", "restored": true})
	blocking.Store(true)
	raw, _ := json.Marshal(map[string]any{"model_id": id, "texts": []string{"q"}})
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	request, _ := http.NewRequestWithContext(ctx, "POST", f.url+"/api/v1/embeddings", bytes.NewReader(raw))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+f.tokens["owner"])
	done := make(chan error, 1)
	go func() {
		response, err := f.client.Do(request)
		if response != nil {
			io.Copy(io.Discard, response.Body)
			response.Body.Close()
		}
		done <- err
	}()
	select {
	case <-entered:
		cancel()
	case <-time.After(3 * time.Second):
		t.Fatal("stored SQL model never reached supplier")
	}
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatal("request cancellation cause lost")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("incoming request failed to drain")
	}
	select {
	case <-stopped:
	case <-time.After(3 * time.Second):
		t.Fatal("bound supplier response/socket not closed")
	}
	if f.snapshot(t) != before {
		t.Fatal("canceled bound inference modified SQL")
	}
}

func TestRoot55VLLMBatchCLIHTTPAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	var vendorCalls atomic.Int64
	var dimensionsMu sync.Mutex
	var dimensions []any
	supplier := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		vendorCalls.Add(1)
		var body map[string]any
		if json.NewDecoder(r.Body).Decode(&body) != nil {
			t.Error("invalid native body")
			return
		}
		if r.URL.Path != "/embeddings" || r.Header.Get("Authorization") != "Bearer vllm-key" {
			t.Error("vllm stored URL/key lost")
		}
		dimensionsMu.Lock()
		dimensions = append(dimensions, body["dimensions"])
		dimensionsMu.Unlock()
		texts := body["input"].([]any)
		rows := make([]any, len(texts))
		for i := range texts {
			rows[len(texts)-1-i] = map[string]any{"index": i, "embedding": []float64{float64(i), 0}}
		}
		json.NewEncoder(w).Encode(map[string]any{"data": rows})
	}))
	f.enroll(t, "vllm", "batch", "vllm-key", supplier.URL, "seed", "chat")
	syntax := "ADD MODEL 'batch-chat batch-embed' TO PROVIDER 'vllm' INSTANCE 'batch' WITH CHAT VISION TOKENS 0, EMBEDDING;"
	command, err := cli.NewParser(syntax).Parse(cli.APIMode)
	if err != nil {
		t.Fatal(err)
	}
	beforeRequests := f.modelMutationCalls.Load()
	if _, err = f.cli(t, "owner").ExecuteCommand(command); err != nil {
		t.Fatal("actual multirow CLI", err)
	}
	if f.modelMutationCalls.Load()-beforeRequests != 1 {
		t.Fatal("CLI batch split into multiple HTTP requests")
	}
	var n int
	if err := f.Read.QueryRow("SELECT count(*) FROM tenant_model m JOIN tenant_model_instance i ON i.id=m.instance_id WHERE i.instance_name='batch' AND m.model_name IN ('batch-chat','batch-embed')").Scan(&n); err != nil || n != 2 {
		t.Fatal("independent CLI batch SQL count", n, err)
	}
	var typ, extra string
	if err := f.Read.QueryRow("SELECT m.model_type,m.extra FROM tenant_model m JOIN tenant_model_instance i ON i.id=m.instance_id WHERE i.instance_name='batch' AND m.model_name='batch-chat'").Scan(&typ, &extra); err != nil {
		t.Fatal(err)
	}
	var metadata map[string]any
	if json.Unmarshal([]byte(extra), &metadata) != nil || typ != "chat" || metadata["max_tokens"] != float64(0) {
		t.Fatal("primary type/explicit zero SQL lost")
	}
	before := f.snapshot(t)
	beforeRequests = f.modelMutationCalls.Load()
	if _, err = f.cli(t, "owner").ExecuteCommand(command); err == nil {
		t.Fatal("duplicate batch succeeded")
	}
	if f.modelMutationCalls.Load()-beforeRequests != 1 || f.snapshot(t) != before {
		t.Fatal("failed batch split or changed SQL")
	}
	var id string
	if err := f.Read.QueryRow("SELECT m.id FROM tenant_model m JOIN tenant_model_instance i ON i.id=m.instance_id WHERE i.instance_name='batch' AND m.model_name='batch-embed'").Scan(&id); err != nil {
		t.Fatal(err)
	}
	for _, option := range []string{"", " DIMENSION 0", " DIMENSION 2"} {
		cmd, err := cli.NewParser(fmt.Sprintf("EMBED TEXT 'a' 'b' WITH '%s'%s;", id, option)).Parse(cli.APIMode)
		if err != nil {
			t.Fatal(err)
		}
		result, err := f.cli(t, "owner").ExecuteCommand(cmd)
		if err != nil {
			t.Fatal(err)
		}
		vectors, ok := result.(*cli.EmbeddingsResponse)
		if !ok || len(vectors.Data) != 2 || vectors.Data[0].Index != 0 || vectors.Data[0].Embedding[0] != 0 || vectors.Data[1].Index != 1 || vectors.Data[1].Embedding[0] != 1 {
			t.Fatal("CLI lost native indexed/zero vectors")
		}
	}
	dimensionsMu.Lock()
	got := append([]any{}, dimensions...)
	dimensionsMu.Unlock()
	if !reflect.DeepEqual(got, []any{nil, nil, float64(2)}) {
		t.Fatal("frozen omitted/zero/explicit dimension wire changed", got)
	}
	count := vendorCalls.Load()
	_, failed, _ := f.request(t, "foreign", "POST", "/api/v1/embeddings", map[string]any{"model_id": id, "texts": []string{"q"}})
	if providerEnvelopeError(failed) == nil || vendorCalls.Load() != count || f.snapshot(t) != before {
		t.Fatal("foreign batch inference side effects")
	}
	cmd, err := cli.NewParser("DROP MODEL 'batch-chat batch-embed' FROM 'vllm' 'batch';").Parse(cli.APIMode)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = f.cli(t, "owner").ExecuteCommand(cmd); err != nil {
		t.Fatal("actual batch delete CLI", err)
	}
	if err := f.Read.QueryRow("SELECT count(*) FROM tenant_model WHERE id=? OR model_name='batch-chat'", id).Scan(&n); err != nil || n != 0 {
		t.Fatal("batch delete independent readback", n, err)
	}
}
