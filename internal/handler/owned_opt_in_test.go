package handler_test

import (
	"crypto/sha256"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"
)

func recordOwnedOptInTestBinary(t *testing.T) {
	t.Helper()
	path, err := os.Executable()
	if err != nil {
		t.Fatal("find actual handler test executable", err)
	}
	file, err := os.Open(path)
	if err != nil {
		t.Fatal("open actual handler test executable", err)
	}
	defer file.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		t.Fatal("hash actual handler test executable", err)
	}
	t.Logf("candidate-handler-sha256 %x", hash.Sum(nil))
}

// The optional manifest lets an acceptance run reserve exact resources before
// creating them. Existing standalone opt-in configurations remain usable.
type ownedOptInResources struct {
	Journal     string            `json:"journal"`
	HTTP        map[string]string `json:"http"`
	Directories map[string]string `json:"directories"`
}

func readOwnedOptInResources(t *testing.T) *ownedOptInResources {
	t.Helper()
	path := os.Getenv("MULTIRAG_GO_OPT_IN_RESOURCES")
	if path == "" {
		return nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("read owned opt-in resource manifest", err)
	}
	var resources ownedOptInResources
	if err := json.Unmarshal(data, &resources); err != nil || !filepath.IsAbs(resources.Journal) {
		t.Fatal("invalid owned opt-in resource manifest", err)
	}
	return &resources
}

func recordOwnedOptInResource(t *testing.T, phase, kind, name string) {
	t.Helper()
	resources := readOwnedOptInResources(t)
	if resources == nil {
		return
	}
	// The caller must create the journal first; never silently create a new one.
	file, err := os.OpenFile(resources.Journal, os.O_WRONLY|os.O_APPEND, 0)
	if err != nil {
		t.Fatal("open owned opt-in journal", err)
	}
	defer file.Close()
	if err := json.NewEncoder(file).Encode(map[string]any{"utc": time.Now().UTC(), "test": t.Name(), "pid": os.Getpid(), "phase": phase, "kind": kind, "name": name}); err != nil {
		t.Fatal("write owned opt-in journal", err)
	}
	if err := file.Sync(); err != nil {
		t.Fatal("sync owned opt-in journal", err)
	}
}

func newOwnedOptInHTTPServer(t *testing.T, name string, handler http.Handler) *httptest.Server {
	t.Helper()
	resources := readOwnedOptInResources(t)
	var server *httptest.Server
	// Register cleanup before starting the server so assertion failures also
	// close it and independently verify that its listener is gone.
	t.Cleanup(func() {
		if server == nil {
			return
		}
		address := server.Listener.Addr().String()
		server.Close()
		conn, err := net.DialTimeout("tcp", address, 200*time.Millisecond)
		if err == nil {
			conn.Close()
			t.Errorf("owned HTTP listener survived cleanup: %s", address)
			return
		}
		t.Log("owned-http-closed-readback", name, address)
		recordOwnedOptInResource(t, "cleanup-readback-absent", "http-listener", address)
	})
	if resources == nil {
		server = httptest.NewServer(handler)
	} else {
		address := resources.HTTP[name]
		host, port, err := net.SplitHostPort(address)
		ip := net.ParseIP(host)
		portNumber, portErr := strconv.Atoi(port)
		if err != nil || ip == nil || !ip.IsLoopback() || portErr != nil || portNumber < 1 || portNumber > 65535 {
			t.Fatal("owned HTTP resource must reserve an exact loopback address", name)
		}
		recordOwnedOptInResource(t, "before-create", "http-listener", address)
		listener, err := net.Listen("tcp", address)
		if err != nil {
			t.Fatal("listen on reserved owned HTTP resource", name, err)
		}
		server = &httptest.Server{Listener: listener, Config: &http.Server{Handler: handler}}
		server.Start()
	}
	t.Log("owned-http-resource", name, server.URL)
	return server
}

func ownedOptInTempDir(t *testing.T, name string) string {
	t.Helper()
	resources := readOwnedOptInResources(t)
	if resources == nil {
		return t.TempDir()
	}
	path := resources.Directories[name]
	if !filepath.IsAbs(path) {
		t.Fatal("owned temporary directory must have an absolute reserved path", name)
	}
	if _, err := os.Lstat(path); !os.IsNotExist(err) {
		t.Fatal("owned temporary directory must not already exist", name, err)
	}
	recordOwnedOptInResource(t, "before-create", "temporary-directory", path)
	if err := os.Mkdir(path, 0700); err != nil {
		t.Fatal("create owned temporary directory", err)
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(path); err != nil {
			t.Error("remove owned temporary directory", err)
		}
		if _, err := os.Lstat(path); !os.IsNotExist(err) {
			t.Error("owned temporary directory survived cleanup", path, err)
			return
		}
		recordOwnedOptInResource(t, "cleanup-readback-absent", "temporary-directory", path)
	})
	return path
}
