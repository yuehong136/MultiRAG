package testsupport

import (
	"crypto/rand"
	"encoding/binary"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// RegisteredHTTPServer reserves a concrete loopback address before binding and
// independently checks closure after draining the server's active connections.
func RegisteredHTTPServer(t testing.TB, handler http.Handler) *httptest.Server {
	t.Helper()
	journal := NewCase(t)
	var listener net.Listener
	var address string
	for attempt := 0; attempt < 30; attempt++ {
		var value [2]byte
		if _, err := rand.Read(value[:]); err != nil {
			t.Fatal(err)
		}
		address = fmt.Sprintf("127.0.0.1:%d", 30000+int(binary.BigEndian.Uint16(value[:]))%20000)
		journal.Event("reserve-http-listener", map[string]any{"address": address})
		var err error
		listener, err = net.Listen("tcp", address)
		if err == nil {
			break
		}
		journal.Event("http-listener-bind-failed", map[string]any{"address": address, "error": err.Error()})
	}
	if listener == nil {
		t.Fatal("unable to bind owned HTTP fixture")
	}
	server := &httptest.Server{Listener: listener, Config: &http.Server{Handler: handler}}
	t.Cleanup(func() {
		server.Close()
		conn, err := net.DialTimeout("tcp", address, 200*time.Millisecond)
		if err == nil {
			conn.Close()
			t.Error("owned HTTP listener survived cleanup")
			return
		}
		journal.Event("http-listener-cleaned", map[string]any{"address": address, "independent_readback": true})
	})
	server.Start()
	return server
}
