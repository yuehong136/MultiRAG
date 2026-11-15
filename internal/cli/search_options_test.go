package cli

import (
	"encoding/json"
	"fmt"
	"net/http"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
)

func TestSearchOptionsParserExecuteHTTPModes(t *testing.T) {
	const syntax = `RETRIEVE 'q' ON DATASETS 'a,b' WITH TOP_K 0 PAGE 2 PAGE_SIZE 3 SIMILARITY_THRESHOLD 0 VECTOR_SIMILARITY_WEIGHT 0 KEYWORD false USE_KG false TENANT_RERANK_ID '9007199254740993' SEARCH_ID 'saved' META_DATA_FILTER '{"method":"manual","manual":[{"key":"serial","op":"is","value":9007199254740993}]}' CROSS_LANGUAGES [] DOC_IDS [];`
	for _, mode := range []string{"normal", "native", "concurrent"} {
		t.Run(mode, func(t *testing.T) {
			var searches atomic.Int32
			client := batchTestClient(t, func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer owned-batch-credential" {
					t.Error("search credential lost")
				}
				if r.URL.Path == "/api/v1/datasets" {
					fmt.Fprint(w, `{"code":0,"data":[{"id":"a-id","name":"a"},{"id":"b-id","name":"b"}]}`)
					return
				}
				if r.URL.Path != "/api/v1/datasets/search" {
					t.Error(r.URL.Path)
				}
				searches.Add(1)
				decoder := json.NewDecoder(r.Body)
				decoder.UseNumber()
				var body map[string]any
				if err := decoder.Decode(&body); err != nil {
					t.Error(err)
				}
				for key, value := range map[string]any{"question": "q", "top_k": json.Number("0"), "page": json.Number("2"), "page_size": json.Number("3"), "similarity_threshold": json.Number("0"), "vector_similarity_weight": json.Number("0"), "keyword": false, "use_kg": false, "tenant_rerank_id": "9007199254740993", "search_id": "saved"} {
					if !reflect.DeepEqual(body[key], value) {
						t.Error("search option lost", key, body[key], value)
					}
				}
				if !reflect.DeepEqual(body["dataset_ids"], []any{"a-id", "b-id"}) || !reflect.DeepEqual(body["doc_ids"], []any{}) || !reflect.DeepEqual(body["cross_languages"], []any{}) {
					t.Error("explicit scope lost", body)
				}
				filter, _ := body["meta_data_filter"].(map[string]any)
				rows, _ := filter["manual"].([]any)
				if len(rows) != 1 || rows[0].(map[string]any)["value"] != json.Number("9007199254740993") {
					t.Error("metadata integer rounded", filter)
				}
				fmt.Fprint(w, `{"code":0,"data":{"chunks":[],"total":0}}`)
			})
			cmd, err := NewParser(syntax).Parse(APIMode)
			if err != nil {
				t.Fatal(err)
			}
			if mode == "native" {
				cmd.Params["iterations"] = 3
			}
			if mode == "concurrent" {
				outer := NewCommand("benchmark")
				outer.Params["command"] = cmd
				outer.Params["iterations"] = 3
				outer.Params["concurrency"] = 2
				if _, err := client.ExecuteCommand(outer); err != nil {
					t.Fatal(err)
				}
			} else {
				result, err := client.ExecuteCommand(cmd)
				if err != nil {
					t.Fatal(err)
				}
				if mode == "native" {
					stats, ok := result.(*BenchmarkResponse)
					if !ok || stats.SuccessCount != 3 || stats.FailureCount != 0 {
						t.Fatal("benchmark requests", result)
					}
				}
			}
			want := map[string]int32{"normal": 1, "native": 3, "concurrent": 6}[mode]
			if searches.Load() != want {
				t.Fatal("search count", searches.Load(), want)
			}
		})
	}
}
func TestSearchOptionsOmittedAndExplicitScopeHTTP(t *testing.T) {
	for _, suffix := range []string{"", " WITH DOC_IDS [] CROSS_LANGUAGES []", " WITH DOC_IDS ['doc-a','doc-b'] CROSS_LANGUAGES ['en','zh'] RERANK_ID 'm@i@p'"} {
		t.Run(suffix, func(t *testing.T) {
			client := batchTestClient(t, func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/api/v1/datasets" {
					fmt.Fprint(w, `{"code":0,"data":[{"name":"a","id":"a-id"}]}`)
					return
				}
				var body map[string]any
				json.NewDecoder(r.Body).Decode(&body)
				if suffix == "" {
					if _, exists := body["doc_ids"]; exists {
						t.Error("omitted doc scope invented")
					}
				} else {
					if _, exists := body["doc_ids"]; !exists {
						t.Error("explicit doc scope omitted")
					}
				}
				fmt.Fprint(w, `{"code":0,"data":{"chunks":[]}}`)
			})
			cmd, err := NewParser("RETRIEVE 'q' ON DATASETS 'a'" + suffix + ";").Parse(APIMode)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := client.ExecuteCommand(cmd); err != nil {
				t.Fatal(err)
			}
		})
	}
}
func TestSearchOptionsInvalidBeforeAnyHTTP(t *testing.T) {
	for _, suffix := range []string{" WITH TOP_K '1'", " WITH PAGE 1.5", " WITH KEYWORD 0", " WITH USE_KG maybe", " WITH META_DATA_FILTER []", " WITH CROSS_LANGUAGES 'en'", " WITH DOC_IDS null", " WITH UNKNOWN 1", "; ignored"} {
		if _, err := NewParser("RETRIEVE 'q' ON DATASETS 'a'" + suffix + ";").Parse(APIMode); err == nil {
			t.Error("malformed option accepted", strings.TrimSpace(suffix))
		}
	}
}
