package cli

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/pem"
	"fmt"
	"github.com/google/uuid"
	"multirag/internal/testsupport"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

func consumerMode(admin bool) CommandLineMode {
	if admin {
		return AdminMode
	}
	return APIMode
}
func consumerTokenPtr(s string) *string { return &s }
func consumerToken(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}
func newConsumerCLI(mode string) *CLI {
	c := consumerCLIWithHTTP(NewHTTPClient())
	if mode == "admin" {
		c.Config.CLIMode = AdminMode
	}
	return c
}
func consumerCLIWithHTTP(client *HTTPClient) *CLI {
	return &CLI{APIServerClientMap: map[string]*HTTPClient{"test": client}, AdminServerClient: client,
		Config: &CommandLineConfig{CLIMode: APIMode, AdminClientConfig: &AdminModeConfig{}, APIClientConfig: APIModeConfig{CurrentAPIServer: "test", APIServerMap: map[string]*APIServerConfig{"test": {Name: "test"}}}}}
}
func consumerHTTP(c *CLI) *HTTPClient {
	if c.Config.CLIMode == AdminMode {
		return c.AdminServerClient
	}
	return c.APIServerClientMap[c.Config.APIClientConfig.CurrentAPIServer]
}

func skillTestClient(t *testing.T, handler http.HandlerFunc) *HTTPClient {
	t.Helper()
	server := providerKeyServer(t, handler)
	parsed, _ := url.Parse(server.URL)
	client := NewHTTPClient()
	client.Host = parsed.Hostname()
	client.Port, _ = strconv.Atoi(parsed.Port())
	client.APIToken = consumerTokenPtr("test-credential")
	return client
}
func consumerFilesystemCommand(input string) bool {
	fields := strings.Fields(input)
	if len(fields) == 0 {
		return false
	}
	switch fields[0] {
	case "ls", "cat", "search", "install", "uninstall":
		return true
	}
	return false
}

type registryTransport struct{}

func (*registryTransport) RoundTrip(*http.Request) (*http.Response, error) {
	return nil, fmt.Errorf("unexpected HTTP request")
}

// Tests establish their own public key exactly as a CLI installation does.
func consumerPublicKey(t *testing.T) {
	t.Helper()
	j := testsupport.NewCase(t)
	root := filepath.Join(os.TempDir(), "canonical-cli-key-"+uuid.NewString())
	j.Event("reserve-cli-key-directory", map[string]any{"path": root})
	if err := os.MkdirAll(filepath.Join(root, "configs", "go"), 0700); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(root); err != nil {
			t.Error(err)
		}
		_, err := os.Lstat(root)
		j.Event("cleanup-cli-key-directory", map[string]any{"path": root, "absent": os.IsNotExist(err)})
		if !os.IsNotExist(err) {
			t.Error("CLI key fixture survived")
		}
	})
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	data, err := x509.MarshalPKIXPublicKey(&key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(filepath.Join(root, "configs/go/public.pem"), pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: data}), 0600); err != nil {
		t.Fatal(err)
	}
	t.Chdir(root)
}
