package cli

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/google/uuid"
	"multirag/internal/testsupport"
)

func registryCLI(t *testing.T) *CLI {
	t.Helper()
	c, err := NewCLIWithArgs(&ConnectionArgs{Host: "127.0.0.1", Port: 9380})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(c.Cleanup)
	return c
}

func registryCommand(t *testing.T, c *CLI, input string) (ResponseIf, error) {
	t.Helper()
	cmd, err := NewParser(input).Parse(c.client.ServerType == "admin")
	if err != nil {
		return nil, err
	}
	response, handled, err := c.executeCommon(cmd)
	if !handled {
		return c.client.ExecuteCommand(cmd)
	}
	return response, err
}

func registryConfigPath(t *testing.T) string {
	t.Helper()
	j := testsupport.NewCase(t)
	path := filepath.Join(os.TempDir(), "cli-registry-"+uuid.NewString())
	j.Event("reserve-private-config", map[string]any{"directory": path, "files": []string{"config.yml", "link.yml"}, "atomic_temporary_pattern": ".multirag-config-*"})
	if err := os.Mkdir(path, 0700); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(path); err != nil {
			t.Fatal(err)
		}
		_, err := os.Lstat(path)
		j.Event("private-config-cleanup-readback", map[string]any{"path": path, "absent": os.IsNotExist(err)})
		if !os.IsNotExist(err) {
			t.Fatal("config survived cleanup")
		}
	})
	return filepath.Join(path, "config.yml")
}

func TestServerRegistryGrammar(t *testing.T) {
	valid := []string{`ADD API SERVER 'b' HOST '127.0.0.1:9000' TOKEN 'private';`, `ADD ADMIN SERVER HOST '127.0.0.1:9001';`, `DELETE API SERVER 'b';`, `DELETE ADMIN SERVER;`, `SHOW API SERVER 'b';`, `SHOW ADMIN SERVER;`, `LIST API SERVER;`, `SHOW CURRENT;`, `SAVE CONFIG AS 'private.yml';`, `LOGIN ADMIN 'a' WITH PASSWORD 'private';`, `RETRIEVE 'q' ON DATASETS 'a';`}
	for _, mode := range []bool{false, true} {
		for _, input := range valid {
			if _, err := NewParser(input).Parse(mode); err != nil {
				t.Fatalf("%s %v: %v", input, mode, err)
			}
		}
	}
	for _, input := range []string{`ADD API SERVER 'x' HOST '127.0.0.1:0';`, `ADD API SERVER 'x' HOST ':10';`, `ADD API SERVER 'x' HOST 'http://bad:10';`, `ADD API SERVER 'x' HOST '127.0.0.1:9000' TOKEN 'a' TOKEN 'b';`, `LIST API SERVER trailing;`, `SHOW CURRENT; PING;`, `SAVE CONFIG AS 'x' trailing;`, `LOGIN ADMIN 'a'; trailing`, `SHOW API SERVER 'b'`, `SWITCH API 'b';`} {
		if _, err := NewParser(input).Parse(false); err == nil {
			t.Fatalf("accepted invalid registry input: %s", input)
		}
	}
	for _, word := range []string{"SERVER", "API", "HOST", "CONFIG", "RETRIEVE"} {
		if !isKeyword(NewLexer(word).NextToken().Type) {
			t.Fatal(word)
		}
	}
}

func TestTrailingGlobalFlagsAndContextOptions(t *testing.T) {
	for _, input := range [][]string{{"LIST USERS", "-o", "json", "--admin"}, {"--admin", "LIST USERS", "--output", "json"}} {
		args, err := ParseConnectionArgs(input)
		if err != nil || !args.AdminMode || args.Port != 9381 || args.Command != "LIST USERS;" || args.OutputFormat != "json" {
			t.Fatalf("args %#v %v", args, err)
		}
	}
	args, err := ParseConnectionArgs([]string{"search", "-q", "query", "-t", "0.5", "-k", "3", "-o", "json"})
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(args.CommandArgs, []string{"search", "-q", "query", "-t", "0.5", "-k", "3"}) || args.APIToken != "" || args.IsSQLMode {
		t.Fatalf("context flags lost: %#v", args)
	}
	for _, args := range [][]string{{"PING", "-o"}, {"PING", "-o", "bad"}, {"-t"}, {"--config"}} {
		if _, err := ParseConnectionArgs(args); err == nil {
			t.Fatal("accepted incomplete flags")
		}
	}
	for _, input := range []string{"ls -n 2", "search --help", "cat file"} {
		if !looksLikeContextEngine(input) {
			t.Fatal(input)
		}
	}
	if _, err := NewParser("ls datasets").Parse(false); err == nil {
		t.Fatal("retired secondary CE grammar survived")
	}
}

func TestRegistryNamedCredentialsModeAndHTTP(t *testing.T) {
	c := registryCLI(t)
	requests := 0
	api := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		if r.Header.Get("Authorization") != "Bearer api-canary" || r.URL.Path != "/api/v1/system/ping" {
			t.Error("wrong API credentials/path")
		}
		fmt.Fprint(w, `{"code":0,"message":"pong"}`)
	}))
	admin := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		if r.Header.Get("Authorization") != "admin-session" || r.URL.Path != "/api/v1/admin/logout" || r.Method != "POST" {
			t.Error("wrong admin credentials/path")
		}
		fmt.Fprint(w, `{"code":0}`)
	}))
	if _, err := registryCommand(t, c, fmt.Sprintf("ADD API SERVER 'b' HOST '%s' TOKEN 'api-canary';", strings.TrimPrefix(api.URL, "http://"))); err != nil {
		t.Fatal(err)
	}
	c.apiServers["default"].HTTPClient.LoginToken = "default-session"
	response, err := registryCommand(t, c, "SHOW API SERVER 'b';")
	if err != nil {
		t.Fatal(err)
	}
	data := response.(*CommonDataResponse).Data
	if data["logged_in"] != false || data["api_token_configured"] != true {
		t.Fatal("named auth read current profile")
	}
	raw, _ := json.Marshal(data)
	if strings.Contains(string(raw), "canary") || strings.Contains(string(raw), "session") {
		t.Fatal("inspection disclosed secret")
	}
	if requests != 0 {
		t.Fatal("local command sent HTTP")
	}
	// Select a named profile internally: SWITCH belongs to a later frozen item.
	c.activeAPI = "b"
	if err := c.selectMode("user"); err != nil {
		t.Fatal(err)
	}
	if err := c.RunSingleCommand("PING;"); err != nil {
		t.Fatal(err)
	}
	if _, err := registryCommand(t, c, "DELETE API SERVER 'b';"); err == nil {
		t.Fatal("deleted active profile")
	}
	if _, err := registryCommand(t, c, fmt.Sprintf("ADD ADMIN SERVER HOST '%s';", strings.TrimPrefix(admin.URL, "http://"))); err != nil {
		t.Fatal(err)
	}
	if err := c.selectMode("admin"); err != nil {
		t.Fatal(err)
	}
	if c.client.HTTPClient.APIKey != "" || c.client.HTTPClient.LoginToken != "" {
		t.Fatal("API credentials crossed into admin")
	}
	c.client.HTTPClient.LoginToken = "admin-session"
	if err := c.RunSingleCommand("LOGOUT;"); err != nil {
		t.Fatal(err)
	}
	if c.client.HTTPClient.LoginToken != "" {
		t.Fatal("logout retained token")
	}
	if err := c.selectMode("user"); err != nil {
		t.Fatal(err)
	}
	if c.client.HTTPClient.APIKey != "api-canary" || c.client.HTTPClient.LoginToken != "" {
		t.Fatal("admin credentials crossed into API")
	}
	if requests != 2 {
		t.Fatalf("requests %d", requests)
	}
	if _, err := registryCommand(t, c, "ADD API SERVER 'b' HOST '127.0.0.1:1';"); err == nil {
		t.Fatal("duplicate overwrote profile")
	}
	if c.client.HTTPClient.APIKey != "api-canary" {
		t.Fatal("failure changed credentials")
	}
}

func TestRegistryPrivateAtomicConfigRoundTrip(t *testing.T) {
	c := registryCLI(t)
	path := registryConfigPath(t)
	if _, err := registryCommand(t, c, `ADD API SERVER 'b' HOST '127.0.0.1:9000' TOKEN 'api-canary';`); err != nil {
		t.Fatal(err)
	}
	if _, err := registryCommand(t, c, `ADD ADMIN SERVER HOST '127.0.0.1:9001';`); err != nil {
		t.Fatal(err)
	}
	c.apiServers["default"].HTTPClient.APIKey = "default-key"
	c.adminClient.HTTPClient.LoginToken = "never-persist-session"
	if _, err := registryCommand(t, c, fmt.Sprintf("SAVE CONFIG AS '%s';", path)); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil || info.Mode().Perm() != 0600 {
		t.Fatal("configuration permissions")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), "never-persist-session") {
		t.Fatal("session persisted")
	}
	args, err := ParseConnectionArgs([]string{"-f", path, "-h", "127.0.0.1:9100", "SHOW CURRENT"})
	if err != nil || args.Port != 9100 {
		t.Fatalf("precedence %#v %v", args, err)
	}
	restored, err := NewCLIWithArgs(args)
	if err != nil {
		t.Fatal(err)
	}
	defer restored.Cleanup()
	if restored.apiServers["b"].HTTPClient.APIKey != "api-canary" || restored.adminClient.HTTPClient.LoginToken != "" {
		t.Fatal("configuration round trip")
	}
	args, err = ParseConnectionArgs([]string{"-f", path, "--admin", "SHOW CURRENT"})
	if err != nil || args.Port != 9001 || args.APIToken != "" {
		t.Fatalf("admin config isolation %#v %v", args, err)
	}
	link := filepath.Join(filepath.Dir(path), "link.yml")
	if err := os.Symlink(path, link); err != nil {
		t.Fatal(err)
	}
	if err := c.saveServerConfig(link); err == nil {
		t.Fatal("followed symlink")
	}
	if err := c.saveServerConfig(filepath.Dir(path)); err == nil {
		t.Fatal("replaced directory")
	}
	if _, err := registryCommand(t, c, fmt.Sprintf("SAVE CONFIG AS '%s' trailing;", path)); err == nil {
		t.Fatal("saved invalid command")
	}
	after, _ := os.ReadFile(path)
	if !reflect.DeepEqual(raw, after) {
		t.Fatal("failed save changed original")
	}
	remaining, _ := filepath.Glob(filepath.Join(filepath.Dir(path), ".multirag-config-*"))
	if len(remaining) != 0 {
		t.Fatal("atomic temp survived")
	}
}

func TestAdminLogoutFailurePreservesSession(t *testing.T) {
	for _, body := range []string{`{"code":100}`, `{}`, `null`, `bad`} {
		server := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
		c := registryCLI(t)
		if _, err := registryCommand(t, c, fmt.Sprintf("ADD ADMIN SERVER HOST '%s';", strings.TrimPrefix(server.URL, "http://"))); err != nil {
			t.Fatal(err)
		}
		if err := c.selectMode("admin"); err != nil {
			t.Fatal(err)
		}
		c.client.HTTPClient.LoginToken = "retained-session"
		if err := c.RunSingleCommand("LOGOUT;"); err == nil || c.client.HTTPClient.LoginToken != "retained-session" {
			t.Fatal("failed logout cleared session")
		}
	}
	var client *HTTPClient
	if _, err := client.Request("GET", "/ping", "", nil, nil); err == nil {
		t.Fatal("nil client succeeded")
	}
	if _, err := (&HTTPClient{Host: "127.0.0.1", Port: 1}).Request("GET", "/ping", "", nil, nil); err == nil {
		t.Fatal("missing transport succeeded")
	}
}

func TestSupportedModelResponseRequiresCanonicalName(t *testing.T) {
	for _, body := range []string{`{}`, `{"code":0}`, `{"code":0,"data":null}`, `{"code":0,"data":["raw"]}`, `{"code":0,"data":[{"model_name":"legacy"}]}`, `{"code":0,"data":[{"name":""}]}`} {
		api := skillTestClient(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) })
		client := &MultiRAGClient{HTTPClient: api, ServerType: "user"}
		cmd, err := NewParser(`LIST SUPPORTED MODELS FROM 'LocalAI' 'one';`).Parse(false)
		if err != nil {
			t.Fatal(err)
		}
		if response, err := client.ExecuteCommand(cmd); err == nil || response != nil {
			t.Fatal("invalid discovery response succeeded")
		}
	}
	for _, body := range []string{`{"code":0,"data":[]}`, `{"code":0,"data":[{"name":"unknown"},{"name":"namespace/raw","max_tokens":0,"dimension":0,"model_types":[],"thinking":{"default_value":false,"clear_thinking":false}}]}`} {
		api := skillTestClient(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) })
		client := &MultiRAGClient{HTTPClient: api, ServerType: "user"}
		cmd, _ := NewParser(`LIST SUPPORTED MODELS FROM 'LocalAI' 'one';`).Parse(false)
		response, err := client.ExecuteCommand(cmd)
		if err != nil {
			t.Fatal(err)
		}
		var expected struct {
			Data []map[string]interface{} `json:"data"`
		}
		if err := json.Unmarshal([]byte(body), &expected); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(response.(*CommonResponse).Data, expected.Data) {
			t.Fatal("metadata changed")
		}
	}
}

func TestRegistryCommandLineEndToEnd(t *testing.T) {
	api := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/system/ping" || r.Header.Get("Authorization") != "Bearer token" {
			t.Error("command-line credentials or path")
		}
		fmt.Fprint(w, `{"code":0,"message":"pong"}`)
	}))
	args, err := ParseConnectionArgs([]string{"-h", strings.TrimPrefix(api.URL, "http://"), "-t", "token", "PING", "-o", "json"})
	if err != nil {
		t.Fatal(err)
	}
	cli, err := NewCLIWithArgs(args)
	if err != nil {
		t.Fatal(err)
	}
	if cli.outputFormat != OutputFormatJSON {
		t.Fatal("lost output format")
	}
	if err := cli.RunSingleCommand(args.Command); err != nil {
		t.Fatal(err)
	}
}
