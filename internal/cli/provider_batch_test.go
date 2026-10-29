package cli

import (
	"encoding/json"
	"fmt"
	"multirag/tests/support"
	"net/http"
	"net/url"
	"reflect"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
)

func batchTestClient(t *testing.T, handler http.HandlerFunc) *MultiRAGClient {
	t.Helper()
	server := support.RegisteredHTTPServer(t, handler)
	address, err := url.Parse(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	client := NewMultiRAGClient("user")
	client.HTTPClient.Host = address.Hostname()
	client.HTTPClient.Port, err = strconv.Atoi(address.Port())
	if err != nil {
		t.Fatal(err)
	}
	client.HTTPClient.APIKey = "owned-batch-credential"
	return client
}

func TestCustomModelBatchParserExecuteHTTP(t *testing.T) {
	var requests atomic.Int32
	client := batchTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/providers/p/instances/i/models" || r.Header.Get("Authorization") != "Bearer owned-batch-credential" {
			t.Error("unexpected batch request", r.Method, r.URL.Path)
		}
		var actual map[string]any
		if err := json.NewDecoder(r.Body).Decode(&actual); err != nil {
			t.Error(err)
		}
		want := map[string]any{
			"provider_name": "p", "instance_name": "i",
			"models": []any{
				map[string]any{"model_name": "a", "model_types": []any{"chat", "image2text"}, "max_tokens": float64(0), "thinking": true},
				map[string]any{"model_name": "b", "model_types": []any{"embedding"}, "max_tokens": float64(0)},
			},
		}
		if !reflect.DeepEqual(actual, want) {
			t.Errorf("batch payload = %#v, want %#v", actual, want)
		}
		fmt.Fprint(w, `{"code":0,"message":"created"}`)
	})
	cmd, err := NewParser(`ADD MODEL 'a b' TO PROVIDER 'p' INSTANCE 'i' WITH CHAT VISION THINK TOKENS 0, EMBEDDING;`).Parse(false)
	if err != nil {
		t.Fatal(err)
	}
	if result, err := client.ExecuteUserCommand(cmd); err != nil || result == nil {
		t.Fatal(result, err)
	}
	if requests.Load() != 1 {
		t.Fatal("batch must use exactly one request", requests.Load())
	}
}

func TestDropRemoveModelBatchParserExecuteHTTP(t *testing.T) {
	for _, syntax := range []string{
		`DROP MODEL 'a b' FROM 'p' 'i';`,
		`REMOVE MODEL 'a b' FROM PROVIDER 'p' INSTANCE 'i';`,
		`REMOVE MODEL 'a b' FROM 'p' 'i'`,
	} {
		t.Run(syntax, func(t *testing.T) {
			var requests atomic.Int32
			client := batchTestClient(t, func(w http.ResponseWriter, r *http.Request) {
				requests.Add(1)
				if r.Method != http.MethodDelete || r.URL.Path != "/api/v1/providers/p/instances/i/models" || r.Header.Get("Authorization") != "Bearer owned-batch-credential" {
					t.Error("unexpected batch deletion", r.Method, r.URL.Path)
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if !reflect.DeepEqual(body, map[string]any{"models": []any{"a", "b"}}) {
					t.Error("batch deletion payload", body)
				}
				fmt.Fprint(w, `{"code":0}`)
			})
			cmd, err := NewParser(syntax).Parse(false)
			if err != nil {
				t.Fatal(err)
			}
			if result, err := client.ExecuteUserCommand(cmd); err != nil || result == nil {
				t.Fatal(result, err)
			}
			if requests.Load() != 1 {
				t.Fatal("batch deletion must use exactly one request", requests.Load())
			}
		})
	}
}

func TestCustomModelInvalidBatchHasZeroHTTP(t *testing.T) {
	var requests atomic.Int32
	client := batchTestClient(t, func(w http.ResponseWriter, _ *http.Request) {
		requests.Add(1)
		w.WriteHeader(http.StatusInternalServerError)
	})
	for _, declaration := range []string{
		`'' WITH CHAT`, `'   ' WITH CHAT`, `'a a' WITH CHAT, EMBEDDING`,
		`'a b' WITH CHAT`, `'a' WITH CHAT, EMBEDDING`, `'a b' WITH CHAT,`, `'a' WITH CHAT,`,
		`'a b' WITH , EMBEDDING`, `'a b' WITH CHAT,, EMBEDDING`,
		`'a' WITH TOKENS 0 TOKENS 1 CHAT`, `'a' WITH TOKENS -1 CHAT`,
		`'a' WITH CHAT CHAT`, `'a' WITH EMBEDDING THINK`, `'a' WITH CHAT THINK THINK`,
		`'a' WITH CHAT UNKNOWN`, `'a' WITH CHAT; trailing`,
	} {
		parts := strings.SplitN(declaration, " WITH ", 2)
		syntax := "ADD MODEL " + parts[0] + " TO PROVIDER 'p' INSTANCE 'i' WITH " + parts[1] + ";"
		cmd, err := NewParser(syntax).Parse(false)
		if err == nil {
			_, _ = client.ExecuteUserCommand(cmd)
			t.Errorf("invalid batch parsed: %s", syntax)
		}
	}
	for _, syntax := range []string{
		`DROP MODEL '' FROM 'p' 'i';`, `REMOVE MODEL 'a a' FROM 'p' 'i';`,
		`DROP MODEL 'a b' FROM 'p' 'i'; trailing`,
	} {
		if cmd, err := NewParser(syntax).Parse(false); err == nil {
			_, _ = client.ExecuteUserCommand(cmd)
			t.Errorf("invalid deletion parsed: %s", syntax)
		}
	}
	for _, models := range [][]customModelDeclaration{
		nil, {},
		{{Name: "a", Types: []string{"chat"}}, {Name: "a", Types: []string{"embedding"}}},
		{{Name: "a", Types: []string{"chat"}, MaxTokens: -1}},
		{{Name: "a", Types: []string{"chat", "chat"}}},
		{{Name: "a", Types: []string{"unknown"}}},
		{{Name: " "}},
	} {
		cmd := NewCommand("add_custom_model")
		cmd.Params["provider_name"], cmd.Params["instance_name"], cmd.Params["models"] = "p", "i", models
		if result, err := client.ExecuteUserCommand(cmd); err == nil || result != nil {
			t.Error("invalid programmatic batch accepted", result, err)
		}
	}
	for _, names := range [][]string{nil, {}, {"a", "a"}, {" "}} {
		cmd := NewCommand("drop_instance_model")
		cmd.Params["provider_name"], cmd.Params["instance_name"], cmd.Params["model_names"] = "p", "i", names
		if result, err := client.ExecuteUserCommand(cmd); err == nil || result != nil {
			t.Error("invalid programmatic deletion accepted", result, err)
		}
	}
	if requests.Load() != 0 {
		t.Fatal("invalid model declaration reached HTTP", requests.Load())
	}
}

func TestEmbeddingOptionalDimensionParserExecuteHTTP(t *testing.T) {
	for _, test := range []struct {
		name, option string
		present      bool
		dimension    float64
	}{
		{name: "omitted"},
		{name: "explicit-default", option: " DIMENSION 0", present: true},
		{name: "explicit-dimension", option: " DIMENSION 2", present: true, dimension: 2},
	} {
		t.Run(test.name, func(t *testing.T) {
			var requests atomic.Int32
			client := batchTestClient(t, func(w http.ResponseWriter, r *http.Request) {
				requests.Add(1)
				if r.Method != http.MethodPost || r.URL.Path != "/api/v1/embeddings" || r.Header.Get("Authorization") != "Bearer owned-batch-credential" {
					t.Error("unexpected inference request", r.Method, r.URL.Path)
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				dimension, present := body["dimension"]
				if present != test.present || (present && dimension != test.dimension) {
					t.Errorf("dimension presence/value = %v/%v", present, dimension)
				}
				if body["model_name"] != "m" || body["instance_name"] != "i" || body["provider_name"] != "p" || !reflect.DeepEqual(body["texts"], []any{"a", "b"}) {
					t.Error("inference scope/texts", body)
				}
				fmt.Fprint(w, `{"code":0,"data":[{"index":1,"embedding":[0,1]},{"index":0,"embedding":[1,0]}]}`)
			})
			cmd, err := NewParser("EMBED TEXT 'a' 'b' WITH 'm@i@p'" + test.option + ";").Parse(false)
			if err != nil {
				t.Fatal(err)
			}
			response, err := client.ExecuteUserCommand(cmd)
			if err != nil {
				t.Fatal(err)
			}
			result, ok := response.(*EmbeddingsResponse)
			if !ok || len(result.Data) != 2 || result.Data[0].Index != 0 || result.Data[1].Index != 1 || !reflect.DeepEqual(result.Data[0].Embedding, []float64{1, 0}) || !reflect.DeepEqual(result.Data[1].Embedding, []float64{0, 1}) {
				t.Fatal("embedding output lost index order", response)
			}
			if requests.Load() != 1 {
				t.Fatal("unexpected inference requests", requests.Load())
			}
		})
	}
	for _, option := range []string{
		"DIMENSION -1;", "DIMENSION 0 DIMENSION 2;", "DIMENSION 1.5;", "DIMENSION;", "; trailing", "DIMENSION 2; trailing", "DIMENSION 2", "UNKNOWN;",
	} {
		if cmd, err := NewParser("EMBED TEXT 'a' WITH 'm@i@p' " + option).Parse(false); err == nil {
			t.Error("invalid inference parsed", option, cmd)
		}
	}
}
