package tool

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"sync"
	"testing"
	"time"
)

var toolResourceMu sync.Mutex

func toolResourceRecord(t *testing.T, event map[string]any) {
	t.Helper()
	path := os.Getenv("MULTIRAG_PROVIDER_TEST_JOURNAL")
	if path == "" {
		if os.Getenv("MULTIRAG_RUN_ID") != "" {
			t.Fatal("registered run requires case journal")
		}
		return
	}
	toolResourceMu.Lock()
	defer toolResourceMu.Unlock()
	f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	event["case"] = t.Name()
	event["parent_run_id"] = os.Getenv("MULTIRAG_RUN_ID")
	if err = json.NewEncoder(f).Encode(event); err != nil {
		t.Fatal(err)
	}
	if err = f.Sync(); err != nil {
		t.Fatal(err)
	}
}

func toolTestListener(t *testing.T) (net.Listener, error) {
	t.Helper()
	for attempt := 0; attempt < 20; attempt++ {
		token := make([]byte, 16)
		if _, err := rand.Read(token); err != nil {
			t.Fatal(err)
		}
		id := hex.EncodeToString(token)
		addr := fmt.Sprintf("127.0.0.1:%d", 20000+(int(token[0])*256+int(token[1]))%30000)
		toolResourceRecord(t, map[string]any{"action": "intent", "run_id": id, "kind": "tool-listener", "address": addr})
		if conn, err := net.DialTimeout("tcp", addr, 20*time.Millisecond); err == nil {
			conn.Close()
			toolResourceRecord(t, map[string]any{"action": "occupied-borrowed", "run_id": id, "address": addr})
			continue
		}
		ln, err := net.Listen("tcp", addr)
		if err != nil {
			toolResourceRecord(t, map[string]any{"action": "allocation-unavailable", "run_id": id, "address": addr})
			continue
		}
		toolResourceRecord(t, map[string]any{"action": "allocated", "run_id": id, "kind": "tool-listener", "address": addr})
		t.Cleanup(func() {
			ln.Close()
			conn, err := net.DialTimeout("tcp", addr, 20*time.Millisecond)
			absent := err != nil
			if conn != nil {
				conn.Close()
			}
			toolResourceRecord(t, map[string]any{"action": "cleanup-readback", "run_id": id, "kind": "tool-listener", "address": addr, "absent": absent})
			if !absent {
				t.Error("owned tool listener remains", addr)
			}
		})
		return ln, nil
	}
	t.Fatal("no unoccupied tool test address")
	return nil, fmt.Errorf("no address")
}

func toolTestUnstartedServer(t *testing.T, handler http.Handler) *httptest.Server {
	t.Helper()
	ln, err := toolTestListener(t)
	if err != nil {
		t.Fatal(err)
	}
	srv := &httptest.Server{Listener: ln, Config: &http.Server{Handler: handler}}
	t.Cleanup(srv.Close)
	return srv
}
func toolTestServer(t *testing.T, handler http.Handler) *httptest.Server {
	t.Helper()
	srv := toolTestUnstartedServer(t, handler)
	srv.Start()
	return srv
}

func toolTestResolver(raw string) (string, net.IP, error) {
	u, err := url.Parse(raw)
	if err != nil {
		return "", nil, err
	}
	ip := net.ParseIP(u.Hostname())
	if ip == nil {
		return "", nil, fmt.Errorf("test resolver accepts literals only")
	}
	return u.Hostname(), ip, nil
}
