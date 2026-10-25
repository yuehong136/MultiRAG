package cli

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strconv"
	"strings"
	"testing"
)

func TestDatasetChunkAndMetadataSyntax(t *testing.T) {
	for input, kind := range map[string]string{
		"GET CHUNK 'c' OF DOCUMENT 'd' IN DATASET 'a name';":                     "get_chunk",
		"LIST METADATA OF DATASET 'a name' 'b';":                                 "list_metadata",
		"REMOVE CHUNKS 'c1' 'c2' FROM DOCUMENT 'd' IN DATASET 'a';":              "remove_chunks",
		"REMOVE ALL CHUNKS FROM DOCUMENT 'd' IN DATASET 'a';":                    "remove_chunks",
		"REMOVE TAGS 'one', 'two' FROM DATASET 'a';":                             "rm_tags",
		`UPDATE CHUNK 'c' OF DOCUMENT 'd' IN DATASET 'a' SET '{"content":"x"}';`: "update_chunk",
	} {
		cmd, err := NewParser(input).Parse(false)
		if err != nil || cmd.Type != kind {
			t.Fatal(input, cmd, err)
		}
	}
	for _, input := range []string{
		"GET CHUNK 'c' OF DATASET 'a';", "GET CHUNK '' OF DOCUMENT 'd' IN DATASET 'a';",
		"GET CHUNK 'c' OF DOCUMENT '' IN DATASET 'a';", "GET CHUNK 'c' OF DOCUMENT 'd' IN DATASET 'a'",
		"GET CHUNK 'c' OF DOCUMENT 'd' IN DATASET 'a'; ignored", "LIST METADATA OF DATASET '';",
		"LIST METADATA OF DATASET 'a' 'a';", "LIST METADATA OF DATASET 'a', 'b';",
		"LIST METADATA OF DATASET 'a'; ignored", "LIST METADATA OF DATASET 'a'",
		"REMOVE CHUNKS 'c1', 'c2' FROM DOCUMENT 'd' IN DATASET 'a';", "REMOVE CHUNKS 'c1' 'c1' FROM DOCUMENT 'd' IN DATASET 'a';",
		"REMOVE CHUNKS '' FROM DOCUMENT 'd' IN DATASET 'a';", "REMOVE CHUNKS 'c' FROM DOCUMENT '';",
		"REMOVE CHUNKS 'c' FROM DOCUMENT 'd' IN DATASET 'a'; ignored", "REMOVE ALL CHUNKS FROM DOCUMENT 'd' IN DATASET 'a'; ignored",
		`UPDATE CHUNK 'c' OF DATASET 'a' SET '{}';`, "REMOVE TAGS 't' FROM DATASET 'a'; ignored",
	} {
		if _, err := NewParser(input).Parse(false); err == nil {
			t.Fatal("invalid syntax accepted", input)
		}
	}
	cmd, _ := NewParser("REMOVE CHUNKS 'c1' 'c2' FROM DOCUMENT 'd' IN DATASET 'a';").Parse(false)
	if !reflect.DeepEqual(cmd.Params["chunk_ids"], []string{"c1", "c2"}) {
		t.Fatal(cmd.Params)
	}
}

func TestDatasetChunkAndMetadataClient(t *testing.T) {
	const a = "9e235004bd8911f19fcaacde48001122"
	const b = "9e235005bd8911f19fcaacde48001122"
	paths := []string{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.Method+" "+r.URL.Path)
		if r.Header.Get("Authorization") != "Bearer owned" {
			t.Error("missing session")
		}
		switch r.URL.Path {
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
	client := NewMultiRAGClient("user")
	client.HTTPClient.Host, client.HTTPClient.Port, client.HTTPClient.LoginToken = address.Hostname(), port, "owned"
	for _, input := range []string{"GET CHUNK 'c?&' OF DOCUMENT 'doc' IN DATASET '" + a + "';", "LIST METADATA OF DATASET '" + a + "' '" + b + "';"} {
		cmd, err := NewParser(input).Parse(false)
		if err != nil {
			t.Fatal(err)
		}
		result, err := client.ExecuteCommand(cmd)
		if err != nil || result == nil || result.TimeCost() <= 0 {
			t.Fatal(result, err)
		}
		if meta, ok := result.(*MetadataResponse); ok {
			rows, err := metadataTable(meta.Data)
			if err != nil || len(rows) != 2 || rows[0]["field"] != "enabled" || rows[1]["value"] != "0" {
				t.Fatal(rows, err)
			}
		}
	}
	if len(paths) != 2 {
		t.Fatal(paths)
	}
}

func TestDatasetObjectsRejectMalformedEnvelopes(t *testing.T) {
	for _, body := range []string{`{}`, `{"code":0,"retcode":1,"data":{}}`, `{"code":500,"data":{}}`, `{"code":0,"data":null}`, `{"code":0,"data":[]}`, `{"code":"0","data":{}}`} {
		if _, err := decodeDatasetObject("test", &Response{StatusCode: 200, Body: []byte(body)}); err == nil {
			t.Fatal(body)
		}
	}
	for _, body := range []string{`{"bad":[]}`, `{"field":{"value":null}}`, `{"field":{"value":[null]}}`, `{"field":{"value":["a","a"]}}`, `{"field":{"value":[""]}}`} {
		var data map[string]any
		if err := json.Unmarshal([]byte(body), &data); err != nil {
			t.Fatal(err)
		}
		if _, err := metadataTable(data); err == nil {
			t.Fatal("invalid metadata accepted", strings.TrimSpace(body))
		}
	}
}

func TestDatasetMutationUsesAPITokenAndRejectsFalseSuccess(t *testing.T) {
	response := `{"code":0,"data":2}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") != "Bearer owned-api" {
			t.Error("mutation lost API token")
		}
		if request.URL.Path != "/api/v1/datasets/9e235004bd8911f19fcaacde48001122/documents/doc/chunks" {
			t.Error("unexpected mutation route", request.URL.Path)
		}
		fmt.Fprint(w, response)
	}))
	defer srv.Close()
	address, _ := url.Parse(srv.URL)
	port, _ := strconv.Atoi(address.Port())
	client := NewMultiRAGClient("user")
	client.HTTPClient.Host, client.HTTPClient.Port, client.HTTPClient.APIKey = address.Hostname(), port, "owned-api"
	command, err := NewParser("REMOVE CHUNKS 'one' 'two' FROM DOCUMENT 'doc' IN DATASET '9e235004bd8911f19fcaacde48001122';").Parse(false)
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.ExecuteCommand(command)
	if err != nil || result.(*SimpleResponse).Message != "2 chunks deleted from doc" {
		t.Fatal(result, err)
	}
	for _, body := range []string{`{}`, `{"code":500,"data":2}`, `{"code":0,"data":-1}`, `{"code":0,"data":0.5}`, `{"code":0,"data":null}`, `{"code":0,"data":{}}`, `{"code":0}`} {
		response = body
		if result, err := client.ExecuteCommand(command); err == nil || result != nil {
			t.Fatal("invalid deletion response became success", body, result, err)
		}
	}
}
