package handler_test

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"sync"
	"testing"
	"time"
)

var mcpHTTPJournalMu sync.Mutex

func mcpHTTPResourceRecord(t *testing.T, record map[string]any) {
	t.Helper()
	path := os.Getenv("MULTIRAG_PROVIDER_TEST_JOURNAL")
	if path == "" {
		if os.Getenv("MULTIRAG_RUN_ID") != "" {
			t.Fatal("registered MCP run requires resource journal")
		}
		return
	}
	mcpHTTPJournalMu.Lock()
	defer mcpHTTPJournalMu.Unlock()
	flags := os.O_APPEND | os.O_WRONLY
	if os.Getenv("MULTIRAG_RUN_ID") == "" {
		flags |= os.O_CREATE
	}
	file, err := os.OpenFile(path, flags, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	record["case"] = t.Name()
	record["parent_run_id"] = os.Getenv("MULTIRAG_RUN_ID")
	if json.NewEncoder(file).Encode(record) != nil || file.Sync() != nil {
		t.Fatal("cannot persist MCP resource identity")
	}
}

func mcpOwnedHTTPServer(t *testing.T, handler http.Handler) *httptest.Server {
	t.Helper()
	var listener net.Listener
	identity := ""
	for attempt := 0; attempt < 10; attempt++ {
		token := make([]byte, 16)
		if _, err := rand.Read(token); err != nil {
			t.Fatal(err)
		}
		identity = hex.EncodeToString(token)
		address := fmt.Sprintf("127.0.0.1:%d", 20000+(int(token[0])*256+int(token[1]))%30000)
		mcpHTTPResourceRecord(t, map[string]any{"action": "intent", "run_id": identity, "kind": "http-listener", "address": address})
		if conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond); err == nil {
			conn.Close()
			mcpHTTPResourceRecord(t, map[string]any{"action": "occupied-borrowed", "run_id": identity, "kind": "http-listener", "address": address})
			continue
		}
		var err error
		listener, err = net.Listen("tcp", address)
		if err == nil {
			break
		}
		mcpHTTPResourceRecord(t, map[string]any{"action": "allocation-unavailable", "run_id": identity, "kind": "http-listener", "address": address})
	}
	if listener == nil {
		t.Fatal("no unoccupied MCP test address")
	}
	address := listener.Addr().String()
	mcpHTTPResourceRecord(t, map[string]any{"action": "allocated", "run_id": identity, "kind": "http-listener", "address": address})
	server := &httptest.Server{Listener: listener, Config: &http.Server{Handler: handler}}
	t.Cleanup(func() {
		server.Close()
		conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
		absent := err != nil
		if conn != nil {
			conn.Close()
		}
		mcpHTTPResourceRecord(t, map[string]any{"action": "cleanup-readback", "run_id": identity, "kind": "http-listener", "address": address, "absent": absent})
		if !absent {
			t.Error("MCP listener remains", address)
		}
	})
	server.Start()
	return server
}
