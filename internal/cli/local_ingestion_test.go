package cli

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"

	"multirag/internal/testsupport"

	"github.com/google/uuid"
)

func TestLocalIngestionCommandsFailClosed(t *testing.T) {
	for _, word := range []string{"PARSE", "FILE", "WITH", "DOC_PARSE", "CHUNK", "RETRIEVE"} {
		if !isKeyword(NewLexer(word).NextToken().Type) {
			t.Fatalf("keyword outside parser bounds: %s", word)
		}
	}
	// These commands need a local ingestion engine. They must not silently use
	// the provider task API or return a successful placeholder response.
	for _, input := range []string{
		`CHUNK 'document.txt' WITH 'options.json';`,
		`EXPLAIN CHUNK 'document.txt' WITH 'options.json';`,
		`PARSE FILE 'document.pdf' VISION 'vision' CHAT 'chat' ASR 'asr' OCR 'ocr' EMBED 'embed' DOC_PARSE 'parser';`,
		`PARSE FILE 'document.pdf' IN DATASET 'knowledge';`,
	} {
		for _, admin := range []bool{false, true} {
			for _, format := range []string{"table", "plain", "json"} {
				args, err := ParseConnectionArgs([]string{input, "-o", format})
				if err != nil || !args.IsSQLMode || looksLikeContextEngine(input) {
					t.Fatalf("local ingestion routed outside SQL: %#v, %v", args, err)
				}
				mode := "user"
				if admin {
					mode = "admin"
				}
				client := NewMultiRAGClient(mode)
				transport := &registryTransport{}
				client.HTTPClient.client.Transport = transport
				client.HTTPClient.APIKey = "controlled"
				client.CurrentModel = &CurrentModel{Model: "pipeline", Instance: "one", Provider: "MinerU"}
				app := &CLI{client: client, args: args, outputFormat: OutputFormat(format)}
				if _, err := NewParser(args.Command).Parse(admin); err == nil {
					t.Fatalf("unsupported local ingestion parsed successfully: %s", input)
				}
				if err := app.RunSingleCommand(args.Command); err == nil || strings.Contains(err.Error(), "unexpected HTTP request") {
					t.Fatalf("unsupported local ingestion escaped parser: %s: %v", input, err)
				}
			}
		}
	}
	for _, kind := range []string{"user_parse_local_file_command", "user_chunk_command"} {
		if result, err := NewMultiRAGClient("user").ExecuteCommand(NewCommand(kind)); err == nil || result != nil {
			t.Fatalf("local ingestion command returned success: %s: %#v, %v", kind, result, err)
		}
	}
}

func TestParseFileTaskHTTPBoundaries(t *testing.T) {
	journal := testsupport.NewCase(t)
	path := filepath.Join(os.TempDir(), "cli-parse-"+uuid.NewString()+".txt")
	journal.Event("reserve-local-input", map[string]any{"path": path})
	if err := os.WriteFile(path, []byte("document bytes"), 0600); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := os.Remove(path); err != nil {
			t.Error(err)
		}
		_, err := os.Lstat(path)
		absent := errors.Is(err, os.ErrNotExist)
		journal.Event("local-input-cleanup-readback", map[string]any{"path": path, "absent": absent})
		if !absent {
			t.Error("local parse input survived cleanup")
		}
	})

	var calls atomic.Int32
	type parseReply struct {
		status int
		body   string
	}
	var reply atomic.Value
	reply.Store(parseReply{http.StatusOK, `{"code":0,"data":{"task_id":"job","status":"pending"}}`})
	server := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/file/parse" || r.URL.RawQuery != "" || r.Header.Get("Authorization") != "Bearer controlled" {
			t.Error("parse route or credentials changed")
		}
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Error(err)
		}
		if len(payload) != 4 || payload["content"] != base64.StdEncoding.EncodeToString([]byte("document bytes")) || payload["provider_name"] != "MinerU" || payload["instance_name"] != "one" || payload["model_name"] != "pipeline" {
			t.Errorf("parse must send file bytes and one model identity: %#v", payload)
		}
		response := reply.Load().(parseReply)
		w.WriteHeader(response.status)
		fmt.Fprint(w, response.body)
	}))
	address, _ := url.Parse(server.URL)
	client := NewMultiRAGClient("user")
	client.HTTPClient.Host = address.Hostname()
	client.HTTPClient.Port, _ = strconv.Atoi(address.Port())
	client.HTTPClient.APIKey = "controlled"
	cmd, err := NewParser(fmt.Sprintf(`PARSE WITH 'pipeline@one@MinerU' FILE %q;`, path)).Parse(false)
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.ExecuteCommand(cmd)
	if err != nil || result == nil || calls.Load() != 1 {
		t.Fatal("file submission failed", result, err, calls.Load())
	}
	rows := result.(*CommonResponse).Data
	if len(rows) != 1 || len(rows[0]) != 2 || rows[0]["task_id"] != "job" || rows[0]["status"] != "pending" {
		t.Fatal("pending task must not pretend to contain parsed segments", rows)
	}
	for _, failure := range []parseReply{
		{http.StatusUnauthorized, `{"code":0,"data":{"task_id":"job","status":"pending"}}`},
		{http.StatusForbidden, `{"code":0,"data":{"task_id":"job","status":"pending"}}`},
		{http.StatusInternalServerError, `{"code":0}`},
		{http.StatusOK, `{"code":403,"message":"denied"}`},
		{http.StatusOK, `{"retcode":103,"retmsg":"denied"}`},
		{http.StatusOK, `{"data":{"task_id":"job","status":"pending"}}`},
		{http.StatusOK, `{"code":0,"data":{"task_id":"job","status":"success"}}`},
		{http.StatusOK, `{"code":0,"data":{"task_id":"job","status":"failed"}}`},
	} {
		reply.Store(failure)
		if result, err := client.ExecuteCommand(cmd); err == nil || result != nil {
			t.Fatalf("failed parse returned success: HTTP %d %s: %#v, %v", failure.status, failure.body, result, err)
		}
	}
	before := calls.Load()
	client.HTTPClient.APIKey = ""
	if result, err := client.ExecuteCommand(cmd); err == nil || result != nil {
		t.Fatal("unauthenticated file submission succeeded", result, err)
	}
	client.HTTPClient.APIKey, client.ServerType = "controlled", "admin"
	if result, err := client.ParseFileCommand(cmd); err == nil || result != nil {
		t.Fatal("admin file submission succeeded", result, err)
	}
	if calls.Load() != before {
		t.Fatal("locally rejected parse called HTTP")
	}
}
