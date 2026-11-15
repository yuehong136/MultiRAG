package cli

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/google/uuid"
	"multirag/internal/testsupport"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"sync/atomic"
	"testing"
)

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
	client := newConsumerCLI("user")
	consumerHTTP(client).Host = address.Hostname()
	consumerHTTP(client).Port, _ = strconv.Atoi(address.Port())
	consumerHTTP(client).APIToken = consumerTokenPtr("controlled")
	cmd, err := NewParser(fmt.Sprintf(`PARSE WITH 'pipeline@one@MinerU' FILE %q;`, path)).Parse(APIMode)
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.ExecuteCommand(cmd)
	if err != nil || result == nil || calls.Load() != 1 {
		t.Fatal("file submission failed", result, err, calls.Load())
	}
	rows := result.(*CommonDataResponse).Data
	if len(rows) != 2 || rows["task_id"] != "job" || rows["status"] != "pending" {
		t.Fatal("pending task must not pretend to contain parsed segments", rows)
	}
	for _, failure := range []parseReply{
		{http.StatusUnauthorized, `{"code":0,"data":{"task_id":"job","status":"pending"}}`},
		{http.StatusForbidden, `{"code":0,"data":{"task_id":"job","status":"pending"}}`},
		{http.StatusInternalServerError, `{"code":0}`},
		{http.StatusOK, `{"code":403,"message":"denied"}`},
		{http.StatusOK, `{"retcode":103,"retmsg":"denied"}`},
		{http.StatusOK, `{"data":{"task_id":"job","status":"pending"}}`},
	} {
		reply.Store(failure)
		if result, err := client.ExecuteCommand(cmd); err == nil || result != nil {
			t.Fatalf("failed parse returned success: HTTP %d %s: %#v, %v", failure.status, failure.body, result, err)
		}
	}
	before := calls.Load()
	consumerHTTP(client).APIToken = nil
	if result, err := client.ExecuteCommand(cmd); err == nil || result != nil {
		t.Fatal("unauthenticated file submission succeeded", result, err)
	}
	consumerHTTP(client).APIToken, client.Config.CLIMode = consumerTokenPtr("controlled"), AdminMode
	if result, err := client.ParseFileUserCommand(cmd); err == nil || result != nil {
		t.Fatal("admin file submission succeeded", result, err)
	}
	if calls.Load() != before {
		t.Fatal("locally rejected parse called HTTP")
	}
}
