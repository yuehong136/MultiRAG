package cli

import (
	"crypto/rand"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"multirag/internal/testsupport"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestProviderKeyGrammar(t *testing.T) {
	for _, input := range []string{
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'private';`,
		`check provider "OpenAI" region "default" key "private";`,
		`CHECK PROVIDER 'LocalAI' REGION '' KEY '' URL 'http://localhost:8000/v1';`,
	} {
		cmd, err := NewParser(input).Parse(APIMode)
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
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'private'; trailing`,
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'private' trailing;`,
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'private';;`,
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'private' KEY 'second';`,
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'private' URL unquoted;`,
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'private' URL 'a' URL 'b';`,
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'unterminated;`,
		`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'private';` + "\x00trailing",
	} {
		if _, err := NewParser(input).Parse(APIMode); err == nil {
			t.Fatalf("accepted invalid case %d", i)
		}
	}
	if _, err := NewParser(`CHECK PROVIDER 'OpenAI' REGION 'default' KEY 'private';`).Parse(AdminMode); err == nil {
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
	client := newConsumerCLI("user")
	consumerHTTP(client).Host = u.Hostname()
	consumerHTTP(client).Port = port
	consumerHTTP(client).LoginToken = consumerTokenPtr("jwt-fixture")
	cmd, err := NewParser(`CHECK PROVIDER 'OpenAI' REGION 'unknown-region' KEY '` + secret + `' URL 'http://localhost:8000/v1';`).Parse(APIMode)
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
	consumerHTTP(client).LoginToken = nil
	if _, err := client.ExecuteCommand(cmd); err == nil {
		t.Fatal("probe without auth accepted")
	}
	consumerHTTP(client).APIToken = consumerTokenPtr("fixture")
	client.Config.CLIMode = AdminMode
	if _, err := client.CheckProviderWithKey(cmd); err == nil {
		t.Fatal("probe in admin mode accepted")
	}
	client.Config.CLIMode = APIMode
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
