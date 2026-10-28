package cli

import (
	"crypto/rand"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"time"

	"multirag/internal/testsupport"

	"github.com/google/uuid"
	"github.com/peterh/liner"
)

func TestProviderKeyGrammar(t *testing.T) {
	for _, input := range []string{
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'private';`,
		`check provider "OpenAI" region "default" key "private";`,
		`CHECK PROVIDER 'LocalAI' REGION '' KEY '' URL 'http://localhost:8000/v1';`,
	} {
		cmd, err := NewParser(input).Parse(false)
		if err != nil || cmd.Type != "check_provider_with_key" {
			t.Fatalf("parse failed: %v", err)
		}
		if _, ok := cmd.Params["api_key"].(string); !ok {
			t.Fatal("key is not a string")
		}
	}
	for i, input := range []string{
		`CHECK PROVIDER OpenAI REGION 'default' KEY 'private';`,
		`CHECK PROVIDER 'OpenAI' KEY 'private';`,
		`CHECK PROVIDER 'OpenAI' REGION default KEY 'private';`,
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY private;`,
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'private'`,
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'private'; trailing`,
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'private' trailing;`,
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'private';;`,
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'private' KEY 'second';`,
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'private' URL unquoted;`,
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'private' URL 'a' URL 'b';`,
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'unterminated;`,
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'private';` + "\x00trailing",
	} {
		if _, err := NewParser(input).Parse(false); err == nil {
			t.Fatalf("accepted invalid case %d", i)
		}
	}
	if _, err := NewParser(`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'private';`).Parse(true); err == nil {
		t.Fatal("admin grammar accepted user probe")
	}
	for _, word := range []string{"CHECK", "PROVIDER", "REGION", "KEY", "URL"} {
		if !isKeyword(NewLexer(word).NextToken().Type) {
			t.Fatalf("not keyword: %s", word)
		}
	}
}

// Reserve each exact address before binding, and independently read it back
// after Close. Ordinary tests and registered runs use the same fixture.
func providerKeyServer(t *testing.T, handler http.Handler) *httptest.Server {
	t.Helper()
	j := testsupport.NewCase(t)
	var listener net.Listener
	var address string
	for attempt := 0; attempt < 30; attempt++ {
		var value [2]byte
		if _, err := rand.Read(value[:]); err != nil {
			t.Fatal(err)
		}
		address = fmt.Sprintf("127.0.0.1:%d", 30000+int(binary.BigEndian.Uint16(value[:]))%20000)
		j.Event("reserve-listener", map[string]any{"address": address})
		var err error
		listener, err = net.Listen("tcp", address)
		if err == nil {
			break
		}
		j.Event("listener-bind-failed", map[string]any{"address": address})
	}
	if listener == nil {
		t.Fatal("cannot bind registered listener")
	}
	server := &httptest.Server{Listener: listener, Config: &http.Server{Handler: handler}}
	t.Cleanup(func() {
		server.Close()
		conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
		absent := err != nil
		if conn != nil {
			conn.Close()
		}
		j.Event("listener-cleanup-readback", map[string]any{"address": address, "absent": absent})
		if !absent {
			t.Error("listener survived close")
		}
	})
	server.Start()
	return server
}

func TestProviderKeyExecute(t *testing.T) {
	const secret = "private-key-canary"
	calls := 0
	server := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Method != "GET" || r.URL.Path != "/api/v1/providers/OpenAI/connection" || r.URL.RawQuery != "" || r.Header.Get("Authorization") != "Bearer jwt-fixture" {
			t.Error("incorrect probe route/auth")
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		want := map[string]any{"region": "unknown-region", "api_key": secret, "base_url": "http://localhost:8000/v1"}
		if !reflect.DeepEqual(body, want) {
			t.Error("region/key/URL payload changed")
		}
		switch calls {
		case 1:
			io.WriteString(w, `{"code":0,"message":"`+secret+`","data":"`+secret+`"}`)
		case 2:
			io.WriteString(w, `{"code":102,"message":"`+secret+`"}`)
		case 3:
			w.WriteHeader(502)
			io.WriteString(w, secret)
		case 4:
			io.WriteString(w, `{"code":"`+secret+`"}`)
		case 5:
			io.WriteString(w, `{}`)
		case 6:
			io.WriteString(w, `{"code":null}`)
		default:
			io.WriteString(w, strings.Repeat(secret, 10000))
		}
	}))
	u, _ := url.Parse(server.URL)
	port, _ := strconv.Atoi(u.Port())
	client := NewMultiRAGClient("user")
	client.HTTPClient.Host = u.Hostname()
	client.HTTPClient.Port = port
	client.HTTPClient.LoginToken = "jwt-fixture"
	cmd, err := NewParser(`CHECK PROVIDER 'OpenAI' REGION 'unknown-region' KEY '` + secret + `' URL 'http://localhost:8000/v1';`).Parse(false)
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.ExecuteCommand(cmd)
	if err != nil {
		t.Fatal(err)
	}
	simple, ok := result.(*SimpleResponse)
	if !ok || simple.Code != 0 || simple.Message != "success" {
		t.Fatal("probe did not return safe standard success")
	}
	for i := 0; i < 6; i++ {
		result, err = client.ExecuteCommand(cmd)
		if err == nil || result != nil || strings.Contains(err.Error(), secret) {
			t.Fatalf("failure %d was successful or leaked key", i)
		}
	}
	if calls != 7 {
		t.Fatalf("calls %d", calls)
	}
	client.HTTPClient.LoginToken = ""
	if _, err := client.ExecuteCommand(cmd); err == nil {
		t.Fatal("probe without auth accepted")
	}
	client.HTTPClient.APIKey = "fixture"
	client.ServerType = "admin"
	if _, err := client.CheckProviderWithKey(cmd); err == nil {
		t.Fatal("probe in admin mode accepted")
	}
	client.ServerType = "user"
	for _, value := range []any{nil, 1, true} {
		cmd.Params["api_key"] = value
		if _, err := client.ExecuteCommand(cmd); err == nil {
			t.Fatal("non-string key accepted")
		}
	}
	if calls != 7 {
		t.Fatal("invalid probe reached HTTP")
	}
}

func TestProviderKeyHistoryAndErrors(t *testing.T) {
	j := testsupport.NewCase(t)
	path := filepath.Join(os.TempDir(), "after33-cli-history-"+uuid.NewString())
	j.Event("reserve-history-file", map[string]any{"path": path})
	t.Cleanup(func() {
		if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
			t.Error(err)
		}
		_, err := os.Lstat(path)
		j.Event("history-cleanup-readback", map[string]any{"path": path, "absent": os.IsNotExist(err)})
		if !os.IsNotExist(err) {
			t.Error("history survived cleanup")
		}
	})
	f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = f.WriteString("LIST DATASETS;\n"); err != nil {
		t.Fatal(err)
	}
	f.Close()
	line := liner.NewLiner()
	defer line.Close()
	c := &CLI{line: line, client: NewMultiRAGClient("user")}
	f, err = os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = line.ReadHistory(f); err != nil {
		t.Fatal(err)
	}
	f.Close()
	for _, input := range []string{
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'private-key-canary';`,
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY private-key-canary;`,
		`CHECK PROVIDER private-key-canary`,
		"CHECK \x00PROVIDER 'OpenAI' REGION 'default' KEY 'private-key-canary';",
		`CREATE PROVIDER 'OpenAI' INSTANCE 'one' KEY 'private-key-canary';`,
	} {
		c.appendHistory(input)
		if err := c.execute(input); err == nil || strings.Contains(err.Error(), "private-key-canary") {
			t.Fatal("credential error was successful or leaked key")
		}
	}
	c.appendHistory("PING;")
	c.saveHistory(path)
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), "private-key-canary") || !strings.Contains(string(raw), "LIST DATASETS;") || !strings.Contains(string(raw), "PING;") {
		t.Fatal("history lost existing entries or persisted credential command")
	}
	j.Event("history-readback", map[string]any{"path": path, "existing_entry_preserved": true, "secret_absent": true})
}
