package cli

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
)

func TestSearchOptionsParserExecuteHTTPModes(t *testing.T) {
	const syntax = `SEARCH 'q' ON DATASETS 'a' 'b' WITH TOP_K 0 PAGE 2 PAGE_SIZE 3 SIMILARITY_THRESHOLD 0 VECTOR_SIMILARITY_WEIGHT 0 KEYWORD false USE_KG false TENANT_RERANK_ID 9007199254740993 SEARCH_ID 'saved' META_DATA_FILTER '{"method":"and","conditions":[]}' CROSS_LANGUAGES [] DOC_IDS [];`
	const expectedJSON = `{"question":"q","dataset_ids":["a-id","b-id"],"top_k":0,"page":2,"size":3,"similarity_threshold":0,"vector_similarity_weight":0,"keyword":false,"use_kg":false,"tenant_rerank_id":9007199254740993,"search_id":"saved","meta_data_filter":{"method":"and","conditions":[]},"cross_languages":[],"doc_ids":[]}`
	for _, mode := range []string{"normal", "native", "concurrent"} {
		t.Run(mode, func(t *testing.T) {
			var searches, lookups atomic.Int32
			client := batchTestClient(t, func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer owned-batch-credential" {
					t.Error("search credential omitted")
				}
				switch r.URL.Path {
				case "/api/v1/datasets":
					lookups.Add(1)
					if r.Method != http.MethodGet {
						t.Error("dataset lookup method", r.Method)
					}
					fmt.Fprint(w, `{"code":0,"data":[{"id":"a-id","name":"a"},{"id":"b-id","name":"b"}]}`)
				case "/api/v1/datasets/search":
					searches.Add(1)
					if r.Method != http.MethodPost {
						t.Error("search method", r.Method)
					}
					decoder := json.NewDecoder(r.Body)
					decoder.UseNumber()
					var actual, expected map[string]any
					if err := decoder.Decode(&actual); err != nil {
						t.Error(err)
					}
					wantDecoder := json.NewDecoder(strings.NewReader(expectedJSON))
					wantDecoder.UseNumber()
					if err := wantDecoder.Decode(&expected); err != nil {
						t.Error(err)
					}
					if !reflect.DeepEqual(actual, expected) {
						t.Errorf("search options lost or changed: %#v, want %#v", actual, expected)
					}
					if actual["tenant_rerank_id"] != json.Number("9007199254740993") {
						t.Error("numeric model ID lost precision", actual["tenant_rerank_id"])
					}
					fmt.Fprint(w, `{"code":0,"data":{"chunks":[]}}`)
				default:
					t.Error("unexpected search route", r.URL.Path)
					http.NotFound(w, r)
				}
			})
			cmd, err := NewParser(syntax).Parse(false)
			if err != nil {
				t.Fatal(err)
			}
			if mode == "native" {
				cmd.Params["iterations"] = 3
			}
			if mode == "concurrent" {
				benchmark := NewCommand("benchmark")
				benchmark.Params["command"] = cmd
				benchmark.Params["iterations"], benchmark.Params["concurrency"] = 3, 2
				if _, err := client.ExecuteCommand(benchmark); err != nil {
					t.Fatal(err)
				}
			} else {
				result, err := client.ExecuteCommand(cmd)
				if err != nil || result == nil {
					t.Fatal(result, err)
				}
				if mode == "native" {
					raw, ok := result.(*BenchmarkRawResponse)
					if !ok || len(raw.ResponseList) != 3 {
						t.Fatal("native search response count", result)
					}
					for _, response := range raw.ResponseList {
						if !isSuccess(response, cmd.Type) {
							t.Fatal("native search rejected successful response")
						}
					}
				}
			}
			want := map[string]int32{"normal": 1, "native": 3, "concurrent": 6}[mode]
			if searches.Load() != want || lookups.Load() == 0 {
				t.Fatalf("search/lookup requests = %d/%d, want %d searches", searches.Load(), lookups.Load(), want)
			}
		})
	}
}

func TestSearchOptionsOmittedAndExplicitScopeHTTP(t *testing.T) {
	for _, suffix := range []string{"", " WITH DOC_IDS [] CROSS_LANGUAGES []", " WITH DOC_IDS ['doc-a','doc-b'] CROSS_LANGUAGES ['en','zh'] RERANK_ID 'reranker@instance@provider'"} {
		t.Run(suffix, func(t *testing.T) {
			var searches atomic.Int32
			client := batchTestClient(t, func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/api/v1/datasets" {
					fmt.Fprint(w, `{"code":0,"data":[{"id":"a-id","name":"a"}]}`)
					return
				}
				if r.URL.Path != "/api/v1/datasets/search" {
					t.Error("unexpected search route", r.URL.Path)
				}
				searches.Add(1)
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				for _, field := range []string{"doc_ids", "cross_languages"} {
					value, present := body[field]
					if suffix == "" {
						if present {
							t.Error("omitted selector unexpectedly sent", field, value)
						}
					} else if !present {
						t.Error("explicit selector omitted", field)
					} else if list, ok := value.([]any); !ok || (strings.Contains(suffix, "[]") && len(list) != 0) || (!strings.Contains(suffix, "[]") && len(list) != 2) {
						t.Error("explicit selector changed", field, value)
					}
				}
				if strings.Contains(suffix, "RERANK_ID") && body["rerank_id"] != "reranker@instance@provider" {
					t.Error("model selector changed", body)
				}
				fmt.Fprint(w, `{"code":0,"data":{"chunks":[]}}`)
			})
			cmd, err := NewParser("SEARCH 'q' ON DATASETS 'a'" + suffix + ";").Parse(false)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := client.ExecuteCommand(cmd); err != nil {
				t.Fatal(err)
			}
			if searches.Load() != 1 {
				t.Fatal("unexpected scope search requests", searches.Load())
			}
		})
	}
}

func TestSearchOptionsInvalidBeforeAnyHTTP(t *testing.T) {
	var requests atomic.Int32
	client := batchTestClient(t, func(w http.ResponseWriter, _ *http.Request) {
		requests.Add(1)
		w.WriteHeader(http.StatusInternalServerError)
	})
	for _, clause := range []string{
		"WITH;", "WITH UNKNOWN 1;", "WITH TOP_K 1 TOP_K 2;", "WITH SIZE 2 PAGE_SIZE 3;",
		"WITH TOP_K '1';", "WITH TOP_K 1.5;", "WITH TOP_K -1;", "WITH PAGE 0;", "WITH PAGE_SIZE 0;",
		"WITH SIMILARITY_THRESHOLD 1.1;", "WITH VECTOR_SIMILARITY_WEIGHT -0.1;", "WITH SIMILARITY_THRESHOLD '0.1';",
		"WITH KEYWORD 'false';", "WITH USE_KG 0;", "WITH KEYWORD maybe;",
		"WITH DOC_IDS '[]';", "WITH DOC_IDS [1];", "WITH DOC_IDS ['a',];", "WITH DOC_IDS ['a','a'];", "WITH CROSS_LANGUAGES [' '];",
		`WITH META_DATA_FILTER 'null';`, `WITH META_DATA_FILTER '[]';`, `WITH META_DATA_FILTER '{bad}';`,
		"WITH TENANT_RERANK_ID 0;", "WITH TENANT_RERANK_ID 9223372036854775808;", "WITH TENANT_RERANK_ID '42';",
		"WITH TENANT_RERANK_ID 42 RERANK_ID 'model';", "WITH RERANK_ID '';",
		"WITH TOP_K 0; trailing", "WITH TOP_K 0", "; trailing",
	} {
		syntax := "SEARCH 'q' ON DATASETS 'a' " + clause
		if cmd, err := NewParser(syntax).Parse(false); err == nil {
			_, _ = client.ExecuteCommand(cmd)
			t.Errorf("invalid search parsed: %s", syntax)
		}
	}
	for _, syntax := range []string{
		"SEARCH '' ON DATASETS 'a';", "SEARCH 'q' ON DATASETS '';", "SEARCH 'q' ON DATASETS 'a,a';", "SEARCH 'q' ON DATASETS 'a' 'a';", "SEARCH 'q' ON DATASETS 'a,,b';",
	} {
		if cmd, err := NewParser(syntax).Parse(false); err == nil {
			_, _ = client.ExecuteCommand(cmd)
			t.Error("invalid dataset scope parsed", syntax)
		}
	}
	for _, options := range []any{
		nil, []string{},
		map[string]any{"unknown": 1}, map[string]any{"top_k": float64(1)}, map[string]any{"page": 0},
		map[string]any{"similarity_threshold": math.NaN()}, map[string]any{"vector_similarity_weight": math.Inf(1)},
		map[string]any{"keyword": "false"}, map[string]any{"use_kg": 0},
		map[string]any{"doc_ids": []string(nil)}, map[string]any{"cross_languages": []string{"en", "en"}},
		map[string]any{"meta_data_filter": map[string]any{}}, map[string]any{"meta_data_filter": "null"},
		map[string]any{"tenant_rerank_id": float64(42)}, map[string]any{"tenant_rerank_id": int64(0)},
		map[string]any{"tenant_rerank_id": int64(42), "rerank_id": "model"}, map[string]any{"size": 1, "page_size": 2},
	} {
		for _, mode := range []string{"normal", "native", "concurrent"} {
			cmd := NewCommand("search_on_datasets")
			cmd.Params["question"], cmd.Params["datasets"], cmd.Params["options"] = "q", "a", options
			if mode == "native" {
				cmd.Params["iterations"] = 3
			}
			if mode == "concurrent" {
				benchmark := NewCommand("benchmark")
				benchmark.Params["command"], benchmark.Params["iterations"], benchmark.Params["concurrency"] = cmd, 3, 2
				cmd = benchmark
			}
			if result, err := client.ExecuteCommand(cmd); err == nil || result != nil {
				t.Error("invalid programmatic search accepted", mode, options, result, err)
			}
		}
	}
	if requests.Load() != 0 {
		t.Fatal("invalid search performed dataset lookup or search HTTP", requests.Load())
	}
}

func TestSearchMetadataGrammarFormatAndContextRouting(t *testing.T) {
	if _, err := NewParser("LIST METADATA OF DATASET 'a';").Parse(false); err == nil {
		t.Fatal("retired LIST METADATA grammar accepted")
	}
	cmd, err := NewParser("GET METADATA OF DATASET 'a' 'b';").Parse(false)
	if err != nil || cmd.Type != "get_metadata" || !reflect.DeepEqual(cmd.Params["dataset_names"], []string{"a", "b"}) {
		t.Fatal("GET METADATA grammar", cmd, err)
	}
	for _, format := range []string{"json", "plain", "table"} {
		cmd, err := NewParser(`\format ` + format).Parse(false)
		if err != nil || cmd.Type != "meta" || cmd.Params["command"] != "format" || !reflect.DeepEqual(cmd.Params["args"], []string{format}) {
			t.Fatal("format grammar changed", cmd, err)
		}
	}
	for _, input := range []string{"search needle datasets", "search --help", "ls datasets", "cat datasets/a/doc"} {
		if !looksLikeContextEngine(input) {
			t.Error("Context Engine command lost routing", input)
		}
	}
	if looksLikeContextEngine("SEARCH 'q' ON DATASETS 'a' WITH TOP_K 0;") {
		t.Fatal("SQL dataset search routed to Context Engine")
	}
	if !isKeyword(TokenPanic) || isKeyword(TokenFloat) || isKeyword(TokenLBracket) || isKeyword(TokenRBracket) {
		t.Fatal("literal additions changed keyword bounds")
	}
}
