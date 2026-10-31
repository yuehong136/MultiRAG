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

func NewHTTP(t testing.TB, handler http.Handler) *httptest.Server {
	t.Helper()
	journal := NewCase(t)
	var listener net.Listener
	address := ""
	for range 30 {
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
		journal.Event("http-bind-failed", map[string]any{"address": address})
	}
	if listener == nil {
		t.Fatal("owned HTTP listener unavailable")
	}
	server := &httptest.Server{Listener: listener, Config: &http.Server{Handler: handler, ReadHeaderTimeout: 5 * time.Second}}
	server.Start()
	t.Cleanup(func() {
		server.Close()
		connection, err := net.DialTimeout("tcp", address, 200*time.Millisecond)
		if err == nil {
			connection.Close()
			t.Error("HTTP listener survived cleanup")
		}
		journal.Event("http-listener-cleaned", map[string]any{"address": address, "absent": err != nil})
	})
	return server
}
