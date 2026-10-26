package service

import (
	"crypto/rand"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"
)

func recordModelTestResource(t *testing.T, action, address string) {
	t.Helper()
	path := os.Getenv("MULTIRAG_PROVIDER_TEST_JOURNAL")
	if path == "" {
		return
	} // Ordinary CI does not claim the local registered-run contract.
	data, err := json.Marshal(map[string]any{"event": action, "test": t.Name(), "address": address, "run_id": os.Getenv("MULTIRAG_RUN_ID"), "pid": os.Getpid()})
	if err != nil {
		t.Fatal(err)
	}
	file, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	if _, err = file.Write(append(data, '\n')); err != nil {
		t.Fatal(err)
	}
	if err = file.Sync(); err != nil {
		t.Fatal(err)
	}
}

func registeredModelServer(t *testing.T, handler http.Handler) *httptest.Server {
	t.Helper()
	var listener net.Listener
	var address string
	for attempt := 0; attempt < 30; attempt++ {
		var random [2]byte
		if _, err := rand.Read(random[:]); err != nil {
			t.Fatal(err)
		}
		address = fmt.Sprintf("127.0.0.1:%d", 30000+int(binary.BigEndian.Uint16(random[:]))%20000)
		recordModelTestResource(t, "reserve-model-listener", address)
		var err error
		listener, err = net.Listen("tcp", address)
		if err == nil {
			break
		}
		recordModelTestResource(t, "model-listener-bind-failed", address)
	}
	if listener == nil {
		t.Fatal("unable to bind an owned model fixture listener")
	}
	server := &httptest.Server{Listener: listener, Config: &http.Server{Handler: handler}}
	t.Cleanup(func() {
		server.Close()
		connection, err := net.DialTimeout("tcp", address, 200*time.Millisecond)
		if err == nil {
			connection.Close()
			t.Error("model fixture listener survived cleanup")
			return
		}
		recordModelTestResource(t, "model-listener-cleaned", address)
	})
	server.Start()
	return server
}
