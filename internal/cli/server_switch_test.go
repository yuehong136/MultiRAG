package cli

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"multirag/internal/testsupport"
)

func TestServerSwitchGrammar(t *testing.T) {
	for _, mode := range []bool{false, true} {
		for _, input := range []string{`USE API 'default';`, `use api "named";`, `USE ADMIN;`} {
			cmd, err := NewParser(input).Parse(mode)
			if err != nil || cmd == nil {
				t.Fatalf("valid switch rejected: %s: %v", input, err)
			}
		}
		for _, input := range []string{
			`USE API;`, `USE API named;`, `USE API '';`, `USE API ' ';`,
			`USE API 'default'`, `USE ADMIN`, `USE API 'default'; PING;`,
			`USE ADMIN;;`, `USE ADMIN trailing;`, `USE ADMIN 'named';`,
			`USE 'API' 'default';`, `USE 'ADMIN';`, `'USE' API 'default';`,
			`USE API 'unterminated;`, `USE API 'default';` + "\x00trailing",
		} {
			if _, err := NewParser(input).Parse(mode); err == nil {
				t.Fatalf("invalid switch accepted: %s", input)
			}
		}
	}
	if cmd, err := NewParser(`USE MODEL 'model@instance@provider';`).Parse(false); err != nil || cmd.Type != "use_model" {
		t.Fatal("existing USE MODEL grammar changed")
	}
}

type switchTransport struct{ closed int }

func (s *switchTransport) RoundTrip(*http.Request) (*http.Response, error) {
	return nil, fmt.Errorf("unexpected request")
}

func (s *switchTransport) CloseIdleConnections() { s.closed++ }

func TestServerSwitchFailureIsAtomicAndClosesIdleTransport(t *testing.T) {
	c := registryCLI(t)
	original := c.client
	original.HTTPClient.LoginToken = "retained-session"
	original.CurrentModel = &CurrentModel{Model: "retained-model"}
	transport := &switchTransport{}
	original.HTTPClient.client.Transport = transport
	engine, prompt := c.contextEngine, c.prompt
	for _, input := range []string{`USE API 'missing';`, `USE ADMIN;`, `USE API 'default'; PING;`, `USE 'ADMIN';`, `\admin`} {
		if err := c.execute(input); err == nil {
			t.Fatal("invalid switch succeeded")
		}
		if c.client != original || c.contextEngine != engine || c.prompt != prompt || c.activeAPI != "default" || c.adminClient != nil || transport.closed != 0 || original.HTTPClient.LoginToken != "retained-session" || original.CurrentModel.Model != "retained-model" {
			t.Fatal("failed switch changed connection or authentication")
		}
	}
	if _, err := registryCommand(t, c, `ADD API SERVER 'ipv6' HOST '[::1]:9480';`); err != nil {
		t.Fatal(err)
	}
	if err := c.execute(`USE API 'ipv6';`); err != nil {
		t.Fatal(err)
	}
	if transport.closed != 1 || c.client.HTTPClient.BuildURL("/probe") != "http://[::1]:9480/api/v1/probe" || c.client.HTTPClient.LoginToken != "" || c.client.CurrentModel != nil {
		t.Fatal("client was not replaced independently or idle transport survived")
	}
	if err := c.execute(`USE API 'default';`); err != nil {
		t.Fatal(err)
	}
	if c.client != original || c.client.HTTPClient.LoginToken != "retained-session" || c.client.CurrentModel.Model != "retained-model" {
		t.Fatal("default profile state was lost")
	}
}

func switchPublicKey(t *testing.T, path string) *rsa.PrivateKey {
	t.Helper()
	j := testsupport.NewCase(t)
	j.Event("reserve-profile-public-key", map[string]any{"path": path, "private_key": "memory only"})
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := x509.MarshalPKIXPublicKey(&key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: encoded}), 0600); err != nil {
		t.Fatal(err)
	}
	return key
}

func TestServerSwitchRoutesAuthContextAndInference(t *testing.T) {
	configPath := registryConfigPath(t)
	configs := map[string]*ServerConfig{}
	calls := map[string]int{}
	for _, name := range []string{"default", "named", "admin"} {
		path := filepath.Join(filepath.Dir(configPath), name+".pem")
		key := switchPublicKey(t, path)
		server := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls[name+r.URL.Path]++
			switch r.URL.Path {
			case "/api/v1/system/ping", "/api/v1/admin/ping":
				fmt.Fprint(w, "pong")
			case "/api/v1/auth/login", "/api/v1/admin/login":
				var body map[string]string
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				ciphertext, err := base64.StdEncoding.DecodeString(body["password"])
				if err != nil {
					t.Error(err)
				}
				plain, err := rsa.DecryptPKCS1v15(rand.Reader, key, ciphertext)
				if err != nil || string(plain) != base64.StdEncoding.EncodeToString([]byte("fixture-password")) || r.Header.Get("Authorization") != "" {
					t.Error("login used another profile's key or credentials")
				}
				w.Header().Set("Authorization", name+"-session")
				fmt.Fprint(w, `{"code":0}`)
			case "/api/v1/datasets", "/api/v1/chat/completions":
				if name == "admin" || r.Header.Get("Authorization") != map[string]string{"default": "default-session", "named": "named-session"}[name] && r.Header.Get("Authorization") != "Bearer "+name+"-session" {
					t.Error("request used another profile's authentication")
				}
				if r.URL.Path == "/api/v1/chat/completions" {
					var body map[string]any
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body["model_name"] != name+"-model" {
						t.Error("inference used another profile's model")
					}
					fmt.Fprint(w, `{"code":0,"data":{"message":"answer"}}`)
				} else {
					fmt.Fprint(w, `{"code":0,"data":[]}`)
				}
			default:
				t.Error("unexpected route")
			}
		}))
		configs[name] = &ServerConfig{Host: strings.TrimPrefix(server.URL, "http://"), PublicKeyFile: path}
	}
	config := &ConfigFile{Host: configs["default"].Host, PublicKeyFile: configs["default"].PublicKeyFile, APIServers: map[string]*ServerConfig{"named": configs["named"]}, AdminServer: configs["admin"]}
	args := &ConnectionArgs{Config: config}
	args.Host, args.Port, _ = validatedServerAddress(config.Host)
	c, err := NewCLIWithArgs(args)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Cleanup()
	for _, name := range []string{"default", "named", "admin", "named", "default"} {
		selection := fmt.Sprintf("USE API '%s';", name)
		login := `LOGIN USER 'owned@example.invalid' WITH PASSWORD 'fixture-password';`
		if name == "admin" {
			selection, login = `USE ADMIN;`, `LOGIN ADMIN 'owned@example.invalid' WITH PASSWORD 'fixture-password';`
		}
		if err := c.execute(selection); err != nil {
			t.Fatal(err)
		}
		if c.client.HTTPClient.LoginToken == "" {
			if err := c.execute(login); err != nil {
				t.Fatal(err)
			}
		}
		if c.client.HTTPClient.LoginToken != name+"-session" || c.client.PublicKeyFile != configs[name].PublicKeyFile {
			t.Fatal("profile authentication or key was lost")
		}
		if name != "admin" {
			if c.client.CurrentModel == nil {
				if err := c.execute(fmt.Sprintf("USE MODEL '%s-model@one@LocalAI';", name)); err != nil {
					t.Fatal(err)
				}
			}
			for _, input := range []string{"ls datasets", `CHAT 'hello';`} {
				if err := c.execute(input); err != nil {
					t.Fatal(err)
				}
			}
		}
	}
	for name := range configs {
		path := "/api/v1/auth/login"
		if name == "admin" {
			path = "/api/v1/admin/login"
		}
		if calls[name+path] != 1 {
			t.Fatal("profile was not logged in exactly once")
		}
	}
	if err := c.saveServerConfig(configPath); err != nil {
		t.Fatal(err)
	}
	restored, err := LoadConfigFileFromPath(configPath)
	if err != nil || restored.PublicKeyFile != config.PublicKeyFile || restored.APIServers["named"].PublicKeyFile != configs["named"].PublicKeyFile || restored.AdminServer.PublicKeyFile != configs["admin"].PublicKeyFile {
		t.Fatal("profile key configuration was not preserved")
	}
}
