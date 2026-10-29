package cli

import (
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"testing"
)

func TestCanonicalStoreAndDeletionGrammar(t *testing.T) {
	for input, kind := range map[string]string{
		`CREATE CHUNK STORE FOR DATASET 'a' VECTOR SIZE 2;`: "create_chunk_store",
		`DROP CHUNK STORE FOR DATASET 'a';`:                 "drop_chunk_store",
		`CREATE METADATA STORE;`:                            "create_metadata_store",
		`DROP METADATA STORE;`:                              "drop_metadata_store",
		`INSERT CHUNKS FROM FILE 'owned.json';`:             "insert_chunks_from_file",
		`DELETE METADATA OF DOCUMENT 'd';`:                  "delete_meta",
		`DELETE METADATA OF DOCUMENT 'd' KEYS '["a.b"]';`:   "delete_meta",
	} {
		cmd, err := NewParser(input).Parse(false)
		if err != nil || cmd.Type != kind {
			t.Fatal(input, cmd, err)
		}
	}
	for _, input := range []string{`DELETE METADATA OF DOCUMENT 'd' KEYS '[]';`, `DELETE METADATA OF DOCUMENT 'd' KEYS 'null';`, `DELETE METADATA OF DOCUMENT 'd' KEYS '["a","a"]';`, `DELETE METADATA OF DOCUMENT 'd' KEYS '[""]';`, `DELETE METADATA OF DOCUMENT 'd'; ignored`, `REMOVE ALL CHUNKS FROM DOCUMENT 'd';`, `GET CHUNK 'c' OF DATASET 'a' DOCUMENT 'd';`, `CREATE DATASET TABLE 'a' VECTOR SIZE 2;`, `DROP METADATA TABLE;`} {
		if _, err := NewParser(input).Parse(false); err == nil {
			t.Fatal("invalid/retired syntax accepted", input)
		}
	}
	for _, word := range []string{"STORE", "CHUNK STORE", "IN"} {
		if !isKeyword(NewLexer(word).NextToken().Type) {
			t.Fatal("keyword not classified", word)
		}
	}
}

func TestMetadataDeleteCLIRequiresAcknowledgment(t *testing.T) {
	response := `{"code":0,"data":true}`
	calls := 0
	srv := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if (r.URL.Path != "/api/v1/document/delete_meta" && r.URL.Path != "/api/v1/document/set_meta") || r.Method != "POST" || r.Header.Get("Authorization") != "Bearer owned" {
			t.Fatal(r.URL, r.Header)
		}
		fmt.Fprint(w, response)
	}))
	defer srv.Close()
	address, _ := url.Parse(srv.URL)
	port, _ := strconv.Atoi(address.Port())
	client := NewMultiRAGClient("user")
	client.HTTPClient.Host = address.Hostname()
	client.HTTPClient.Port = port
	client.HTTPClient.APIKey = "owned"
	command, _ := NewParser(`DELETE METADATA OF DOCUMENT 'd' KEYS '["a"]';`).Parse(false)
	if _, err := client.ExecuteCommand(command); err != nil {
		t.Fatal(err)
	}
	for _, bad := range []string{`{}`, `{"code":0}`, `{"code":0,"data":false}`, `{"code":0,"data":null}`, `{"code":0,"retcode":1,"data":true}`, `{"code":500,"data":true}`} {
		response = bad
		if result, err := client.ExecuteCommand(command); err == nil || result != nil {
			t.Fatal("false success", bad, result, err)
		}
	}
	set, _ := NewParser(`SET METADATA OF DOCUMENT 'd' TO '{"zero":0}';`).Parse(false)
	response = `{"code":0,"data":true}`
	if _, err := client.ExecuteCommand(set); err != nil {
		t.Fatal(err)
	}
	response = `{"code":0,"data":false}`
	if _, err := client.ExecuteCommand(set); err == nil {
		t.Fatal("set metadata false success")
	}
	before := calls
	command.Params["keys"] = "[]"
	if _, err := client.ExecuteCommand(command); err == nil || calls != before {
		t.Fatal("invalid selection reached HTTP")
	}
}

func TestBedrockInstanceURLDoesNotInventAWSRegion(t *testing.T) {
	command, err := NewParser(`CREATE PROVIDER 'Bedrock' INSTANCE 'office' KEY '{"bedrock_region":"us-east-1"}' URL 'http://127.0.0.1:12345';`).Parse(false)
	if err != nil || command.Params["region"] != nil {
		t.Fatal("instance name replaced credential region", command, err)
	}
}
