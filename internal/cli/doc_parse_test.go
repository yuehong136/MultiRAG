package cli

import (
	"encoding/json"
	"fmt"
	"net/http"
	"reflect"
	"sync/atomic"
	"testing"
)

func TestDocParseModelDeclaration(t *testing.T) {
	for _, test := range []struct {
		name      string
		options   string
		maxTokens int
	}{
		{"explicit limit", "DOC_PARSE TOKENS 8192;", 8192},
		{"tokens before capability", "TOKENS 8192 DOC_PARSE;", 8192},
		{"explicit zero", "DOC_PARSE TOKENS 0;", 0},
		{"default zero", "DOC_PARSE;", 0},
	} {
		t.Run(test.name, func(t *testing.T) {
			cmd, err := NewParser("ADD MODEL 'pipeline' TO PROVIDER 'mineru_local' INSTANCE 'one' WITH " + test.options).Parse(false)
			if err != nil {
				t.Fatal(err)
			}
			wantModels := []customModelDeclaration{{Name: "pipeline", Types: []string{"doc_parse"}, MaxTokens: test.maxTokens}}
			if cmd.Type != "add_custom_model" || cmd.Params["provider_name"] != "mineru_local" || cmd.Params["instance_name"] != "one" || !reflect.DeepEqual(cmd.Params["models"], wantModels) {
				t.Fatalf("document parsing declaration = %#v", cmd)
			}
			var requests atomic.Int32
			client := batchTestClient(t, func(w http.ResponseWriter, r *http.Request) {
				requests.Add(1)
				if r.Method != http.MethodPost || r.URL.Path != "/api/v1/providers/mineru_local/instances/one/models" || r.Header.Get("Authorization") != "Bearer owned-batch-credential" {
					t.Error("unexpected document parsing declaration request")
				}
				var actual map[string]any
				if err := json.NewDecoder(r.Body).Decode(&actual); err != nil {
					t.Error(err)
				}
				want := map[string]any{
					"provider_name": "mineru_local", "instance_name": "one",
					"models": []any{map[string]any{"model_name": "pipeline", "model_types": []any{"doc_parse"}, "max_tokens": float64(test.maxTokens)}},
				}
				if !reflect.DeepEqual(actual, want) {
					t.Errorf("document parsing payload = %#v, want %#v", actual, want)
				}
				fmt.Fprint(w, `{"code":0}`)
			})
			if result, err := client.ExecuteUserCommand(cmd); err != nil || result == nil {
				t.Fatal(result, err)
			}
			if requests.Load() != 1 {
				t.Fatal("document parsing declaration must use one request", requests.Load())
			}
		})
	}
	var requests atomic.Int32
	client := batchTestClient(t, func(w http.ResponseWriter, _ *http.Request) {
		requests.Add(1)
		w.WriteHeader(http.StatusInternalServerError)
	})
	for _, suffix := range []string{
		"DOC_PARSE DOC_PARSE TOKENS 1;", "DOC_PARSE THINK TOKENS 1;",
		"DOC_PARSE TOKENS -1;", "DOC_PARSE TOKENS 1.5;", "DOC_PARSE TOKENS 0 TOKENS 1;",
		"DOC_PARSE TOKENS 1; ignored", "DOC_PARSE TOKENS 1",
	} {
		if cmd, err := NewParser("ADD MODEL 'pipeline' TO PROVIDER 'mineru_local' INSTANCE 'one' WITH " + suffix).Parse(false); err == nil {
			_, _ = client.ExecuteUserCommand(cmd)
			t.Errorf("invalid document parsing declaration accepted: %s", suffix)
		}
	}
	thinking := true
	for _, declaration := range []customModelDeclaration{
		{Name: "pipeline", Types: []string{"doc_parse", "doc_parse"}},
		{Name: "pipeline", Types: []string{"doc_parse"}, Thinking: &thinking},
	} {
		cmd := NewCommand("add_custom_model")
		cmd.Params["provider_name"], cmd.Params["instance_name"] = "mineru_local", "one"
		cmd.Params["models"] = []customModelDeclaration{declaration}
		if result, err := client.ExecuteUserCommand(cmd); err == nil || result != nil {
			t.Error("invalid programmatic document parsing declaration accepted", result, err)
		}
	}
	if requests.Load() != 0 {
		t.Fatal("invalid document parsing declaration reached HTTP", requests.Load())
	}
}
