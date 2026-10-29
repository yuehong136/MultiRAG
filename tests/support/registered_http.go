package support

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

var httpJournalMu sync.Mutex

func recordHTTPResource(t *testing.T, resourceID, action, address string, absent *bool) {
	t.Helper()
	journal := os.Getenv("MULTIRAG_PROVIDER_TEST_JOURNAL")
	if journal == "" {
		if os.Getenv("MULTIRAG_RUN_ID") != "" {
			t.Fatal("registered HTTP fixture requires its resource journal")
		}
		return
	}
	inputJournal := os.Getenv("MULTIRAG_RUN_JOURNAL")
	if inputJournal == "" {
		inputJournal = journal
	}
	record := map[string]any{
		"action": action, "kind": "http-listener", "resource_id": resourceID,
		"address": address, "effective_endpoint": "http://" + address,
		"case": t.Name(), "parent_run_id": os.Getenv("MULTIRAG_RUN_ID"),
		"inputs_reference": inputJournal + "#reserve-run.inputs",
	}
	if absent != nil {
		record["absent"] = *absent
	}
	httpJournalMu.Lock()
	defer httpJournalMu.Unlock()
	file, err := os.OpenFile(journal, os.O_WRONLY|os.O_APPEND, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	if err := json.NewEncoder(file).Encode(record); err != nil {
		t.Fatal(err)
	}
	if err := file.Sync(); err != nil {
		t.Fatal(err)
	}
}

// RegisteredHTTPServer reserves an exact listener before binding it. Its
// cleanup independently reads back that the owned endpoint no longer accepts
// connections. Source and configuration hashes are retained by the run journal.
func RegisteredHTTPServer(t *testing.T, handler http.Handler) *httptest.Server {
	t.Helper()
	var listener net.Listener
	var resourceID, address string
	for attempt := 0; attempt < 30; attempt++ {
		var token [16]byte
		if _, err := rand.Read(token[:]); err != nil {
			t.Fatal(err)
		}
		resourceID = hex.EncodeToString(token[:])
		address = fmt.Sprintf("127.0.0.1:%d", 20000+(int(token[0])*256+int(token[1]))%30000)
		recordHTTPResource(t, resourceID, "reserve", address, nil)
		if connection, err := net.DialTimeout("tcp", address, 100*time.Millisecond); err == nil {
			connection.Close()
			recordHTTPResource(t, resourceID, "occupied-borrowed", address, nil)
			continue
		}
		var err error
		listener, err = net.Listen("tcp", address)
		if err == nil {
			break
		}
		recordHTTPResource(t, resourceID, "bind-unavailable", address, nil)
	}
	if listener == nil {
		t.Fatal("no unoccupied address for registered HTTP fixture")
	}
	t.Cleanup(func() { listener.Close() })
	recordHTTPResource(t, resourceID, "allocated", address, nil)
	server := &httptest.Server{Listener: listener, Config: &http.Server{Handler: handler}}
	t.Cleanup(func() {
		server.Close()
		connection, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
		absent := err != nil
		if connection != nil {
			connection.Close()
		}
		recordHTTPResource(t, resourceID, "cleanup-readback", address, &absent)
		if !absent {
			t.Error("registered HTTP listener survived cleanup", address)
		}
	})
	server.Start()
	return server
}
