package handler_test

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"

	"multirag/internal/cli"
	"multirag/internal/testsupport"
)

func TestGoogleEmbeddingLiveHTTPAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	var calls atomic.Int64
	var malformed atomic.Bool
	upstream := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		instance := strings.Split(strings.TrimPrefix(r.URL.Path, "/"), "/")[0]
		if instance != "one" && instance != "two" || r.Header.Get("x-goog-api-key") != "controlled-"+instance || r.URL.Path != "/"+instance+"/v1beta/models/text-embedding-004:batchEmbedContents" {
			t.Error("Google selected instance/endpoint/key lost", r.URL.Path)
		}
		var body struct {
			Requests []struct {
				Content              struct{ Parts []struct{ Text string } }
				OutputDimensionality int
			}
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil || len(body.Requests) != 2 {
			t.Error("native batch payload", err)
			w.WriteHeader(400)
			return
		}
		for _, request := range body.Requests {
			if request.OutputDimensionality != 2 {
				t.Error("dimension lost")
			}
		}
		if malformed.Load() {
			fmt.Fprint(w, `{"embeddings":[{"values":[1,null]},{"values":[2,3]}]}`)
		} else {
			fmt.Fprint(w, `{"embeddings":[{"values":[0,1]},{"values":[2,0]}]}`)
		}
	}))
	ids := map[string]string{}
	for _, instance := range []string{"one", "two"} {
		ids[instance] = f.enroll(t, "Google", instance, "controlled-"+instance, upstream.URL+"/"+instance, "text-embedding-004", "embedding")
	}
	before := f.snapshot(t)
	for _, instance := range []string{"one", "two"} {
		result := f.requireSuccess(t, "owner", "POST", "/api/v1/embeddings", map[string]any{"model_id": ids[instance], "texts": []string{"first", "second"}, "dimension": 2})
		rows := result["data"].([]any)
		if len(rows) != 2 || rows[0].(map[string]any)["index"] != float64(0) || rows[0].(map[string]any)["embedding"].([]any)[0] != float64(0) || rows[1].(map[string]any)["embedding"].([]any)[1] != float64(0) {
			t.Fatal("native embedding order/zeros", result)
		}
	}
	if calls.Load() != 2 {
		t.Fatal("one native batch per instance", calls.Load())
	}
	command, err := cli.NewParser(fmt.Sprintf("EMBED TEXT 'first' 'second' WITH '%s' DIMENSION 2;", ids["one"])).Parse(cli.APIMode)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.cli(t, "owner").ExecuteCommand(command); err != nil {
		t.Fatal("actual embedding CLI", err)
	}
	count := calls.Load()
	for _, actor := range []string{"", "foreign", "inactive", "revoked", "invited", "member", "disabledtenant"} {
		_, result, _ := f.request(t, actor, "POST", "/api/v1/embeddings", map[string]any{"model_id": ids["one"], "texts": []string{"first", "second"}, "dimension": 2})
		if providerEnvelopeError(result) == nil {
			t.Fatal("denied Google inference", actor)
		}
	}
	if calls.Load() != count || f.snapshot(t) != before {
		t.Fatal("denied embedding had provider/SQL effects")
	}
	malformed.Store(true)
	_, result, raw := f.request(t, "owner", "POST", "/api/v1/embeddings", map[string]any{"model_id": ids["one"], "texts": []string{"first", "second"}, "dimension": 2})
	if providerEnvelopeError(result) == nil || strings.Contains(raw, "controlled-") {
		t.Fatal("malformed Google vector accepted/leaked", raw)
	}
	f.Journal.Event("canonical-google-embedding-readback", map[string]any{"actual_http_mysql_cli": true, "two_instances_native_sdk": true, "single_batch_literal_zero_order": true, "malformed_vectors_fail": true, "denied_zero_provider_sql_side_effects": true})
}
