package cli

import (
	"fmt"
	"multirag/tests/support"
	"net/http"
	"net/url"
	"reflect"
	"strconv"
	"testing"
)

func TestDatasetChunkAndMetadataSyntax(t *testing.T) {
	for input, kind := range map[string]string{
		"GET CHUNK 'c' OF DOCUMENT 'd' IN DATASET 'a name';":                     "get_chunk",
		"GET METADATA OF DATASET 'a name' 'b';":                                  "get_metadata",
		"REMOVE CHUNKS 'c1' 'c2' FROM DOCUMENT 'd' IN DATASET 'a';":              "remove_chunks",
		"REMOVE ALL CHUNKS FROM DOCUMENT 'd' IN DATASET 'a';":                    "remove_chunks",
		"REMOVE TAGS 'one', 'two' FROM DATASET 'a';":                             "rm_tags",
		`UPDATE CHUNK 'c' OF DOCUMENT 'd' IN DATASET 'a' SET '{"content":"x"}';`: "update_chunk",
	} {
		cmd, err := NewParser(input).Parse(APIMode)
		if err != nil || cmd.Type != kind {
			t.Fatal(input, cmd, err)
		}
	}
	for _, input := range []string{
		"GET CHUNK 'c' OF DATASET 'a';", "GET CHUNK 'c' OF DOCUMENT 'd' IN DATASET 'a'; ignored",
		"GET METADATA OF DATASET 'a'; ignored", "REMOVE CHUNKS 'c' FROM DOCUMENT 'd' IN DATASET 'a'; ignored",
		"REMOVE ALL CHUNKS FROM DOCUMENT 'd' IN DATASET 'a'; ignored", `UPDATE CHUNK 'c' OF DATASET 'a' SET '{}';`,
		"REMOVE TAGS 't' FROM DATASET 'a'; ignored",
	} {
		if _, err := NewParser(input).Parse(APIMode); err == nil {
			t.Fatal("invalid syntax accepted", input)
		}
	}
	cmd, _ := NewParser("REMOVE CHUNKS 'c1' 'c2' FROM DOCUMENT 'd' IN DATASET 'a';").Parse(APIMode)
	if !reflect.DeepEqual(cmd.Params["chunk_ids"], []string{"c1", "c2"}) {
		t.Fatal(cmd.Params)
	}
}

func TestDatasetChunkAndMetadataClient(t *testing.T) {
	const a = "9e235004bd8911f19fcaacde48001122"
	const b = "9e235005bd8911f19fcaacde48001122"
	paths := []string{}
	srv := support.RegisteredHTTPServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.Method+" "+r.URL.Path)
		if r.Header.Get("Authorization") != "owned" {
			t.Error("missing session")
		}
		switch r.URL.Path {
		case "/api/v1/datasets":
			fmt.Fprint(w, `{"code":0,"data":[{"id":"`+a+`","name":"`+a+`"},{"id":"`+b+`","name":"`+b+`"}]}`)
		case "/api/v1/datasets/" + a + "/documents/doc/chunks/c?&":
			if r.URL.RawQuery != "" {
				t.Error("chunk ID escaped into query")
			}
			fmt.Fprint(w, `{"code":0,"data":{"id":"c?&","doc_id":"doc","kb_id":"`+a+`","content_with_weight":"body"}}`)
		case "/api/v1/datasets/metadata/flattened":
			if r.URL.Query().Get("dataset_ids") != a+","+b {
				t.Error("lost dataset scope", r.URL.String())
			}
			fmt.Fprint(w, `{"code":0,"data":{"rank":{"0":["doc"]},"enabled":{"false":["doc"]}}}`)
		default:
			t.Error("obsolete or unexpected route", r.URL.String())
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()
	address, _ := url.Parse(srv.URL)
	port, _ := strconv.Atoi(address.Port())
	client := newConsumerCLI("user")
	consumerHTTP(client).Host, consumerHTTP(client).Port, consumerHTTP(client).LoginToken = address.Hostname(), port, consumerTokenPtr("owned")
	for _, input := range []string{"GET CHUNK 'c?&' OF DOCUMENT 'doc' IN DATASET '" + a + "';", "GET METADATA OF DATASET '" + a + "' '" + b + "';"} {
		cmd, err := NewParser(input).Parse(APIMode)
		if err != nil {
			t.Fatal(err)
		}
		result, err := client.ExecuteCommand(cmd)
		if err != nil || result == nil || result.TimeCost() <= 0 {
			t.Fatal(result, err)
		}
		if meta, ok := result.(*MetadataResponse); ok {
			if len(meta.Data) != 2 {
				t.Fatal("metadata fields lost", meta.Data)
			}
		}
	}
	if len(paths) != 4 {
		t.Fatal(paths)
	}
}

func TestDatasetMutationUsesAPITokenAndRejectsFalseSuccess(t *testing.T) {
	response := `{"code":0,"data":2}`
	srv := support.RegisteredHTTPServer(t, http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") != "Bearer owned-api" {
			t.Error("mutation lost API token")
		}
		if request.URL.Path == "/api/v1/datasets" {
			fmt.Fprint(w, `{"code":0,"data":[{"id":"9e235004bd8911f19fcaacde48001122","name":"9e235004bd8911f19fcaacde48001122"}]}`)
			return
		}
		if request.URL.Path != "/api/v1/datasets/9e235004bd8911f19fcaacde48001122/documents/doc/chunks" {
			t.Error("unexpected mutation route", request.URL.Path)
		}
		fmt.Fprint(w, response)
	}))
	defer srv.Close()
	address, _ := url.Parse(srv.URL)
	port, _ := strconv.Atoi(address.Port())
	client := newConsumerCLI("user")
	consumerHTTP(client).Host, consumerHTTP(client).Port, consumerHTTP(client).APIToken = address.Hostname(), port, consumerTokenPtr("owned-api")
	command, err := NewParser("REMOVE CHUNKS 'one' 'two' FROM DOCUMENT 'doc' IN DATASET '9e235004bd8911f19fcaacde48001122';").Parse(APIMode)
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.ExecuteCommand(command)
	if err != nil || result.(*SimpleResponse).Message != "Success to remove chunks from document doc: 2 chunks deleted" {
		t.Fatal(result, err)
	}
	for _, body := range []string{`{}`, `{"code":500,"data":2}`, `{"code":0,"data":-1}`, `{"code":0,"data":0.5}`, `{"code":0,"data":null}`, `{"code":0,"data":{}}`, `{"code":0}`} {
		response = body
		if result, err := client.ExecuteCommand(command); err == nil || result != nil {
			t.Fatal("invalid deletion response became success", body, result, err)
		}
	}
}
