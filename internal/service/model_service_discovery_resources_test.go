package service

import (
	"fmt"
	"multirag/internal/testsupport"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func serviceDiscoveryTestServer(t *testing.T, h http.Handler) *httptest.Server {
	t.Helper()
	j := testsupport.NewCase(t)
	label := t.Name()
	var listener net.Listener
	for range 20 {
		address := fmt.Sprintf("127.0.0.1:%d", 21000+time.Now().UnixNano()%29000)
		j.Event("reserve-listener", map[string]any{"label": label, "address": address})
		if conn, err := net.DialTimeout("tcp", address, 50*time.Millisecond); err == nil {
			conn.Close()
			continue
		}
		var err error
		listener, err = net.Listen("tcp", address)
		if err == nil {
			break
		}
	}
	if listener == nil {
		t.Fatal("no free registered listener")
	}
	address := listener.Addr().String()
	srv := &httptest.Server{Listener: listener, Config: &http.Server{Handler: h}}
	srv.Start()
	t.Cleanup(func() {
		srv.CloseClientConnections()
		srv.Close()
		conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
		if conn != nil {
			conn.Close()
		}
		j.Event("cleanup-listener", map[string]any{"label": label, "address": address, "absent": err != nil})
		if err == nil {
			t.Error("listener remains", address)
		}
	})
	return srv
}
