package handler

import (
	"bufio"
	"crypto/rand"
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"multirag/internal/testsupport"
)

// Register the exact socket before bind; the listener belongs to the actual
// test PID, rather than the outer go command's process group.
func streamingHTTPServer(t *testing.T, handler http.Handler, timeout time.Duration) *httptest.Server {
	t.Helper()
	journal := testsupport.NewCase(t)
	var listener net.Listener
	var address string
	for attempt := 0; attempt < 30; attempt++ {
		var random [2]byte
		if _, err := rand.Read(random[:]); err != nil {
			t.Fatal(err)
		}
		address = fmt.Sprintf("127.0.0.1:%d", 30000+int(binary.BigEndian.Uint16(random[:]))%20000)
		journal.Event("reserve-listener", map[string]any{"address": address, "pid": os.Getpid(), "pgid": syscall.Getpgrp()})
		var err error
		listener, err = net.Listen("tcp", address)
		if err == nil {
			break
		}
		journal.Event("listener-bind-failed", map[string]any{"address": address})
	}
	if listener == nil {
		t.Fatal("cannot bind registered HTTP listener")
	}
	server := &httptest.Server{Listener: listener, Config: &http.Server{Handler: handler, WriteTimeout: timeout}}
	t.Cleanup(func() {
		server.Close()
		conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
		if err == nil {
			conn.Close()
			t.Error("listener survived cleanup")
		}
		journal.Event("listener-cleanup-readback", map[string]any{"address": address, "absent": err != nil})
	})
	server.Start()
	server.Client().Timeout = 3 * time.Second
	journal.Event("listener-started", map[string]any{"address": address, "pid": os.Getpid(), "pgid": syscall.Getpgrp(), "write_timeout": timeout.String()})
	return server
}

func TestSSEWriteDeadlineHTTP(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, disable := range []bool{false, true} {
		t.Run(fmt.Sprint(disable), func(t *testing.T) {
			router := gin.New()
			router.GET("/stream", func(c *gin.Context) {
				if disable {
					disableWriteDeadlineForSSE(c)
				}
				c.Header("Content-Type", "text/event-stream")
				_, _ = io.WriteString(c.Writer, "data: first\n\n")
				c.Writer.Flush()
				select {
				case <-time.After(150 * time.Millisecond):
				case <-c.Request.Context().Done():
					return
				}
				_, _ = io.WriteString(c.Writer, "data: second\n\n")
				c.Writer.Flush()
			})
			server := streamingHTTPServer(t, router, 40*time.Millisecond)
			resp, err := server.Client().Get(server.URL + "/stream")
			if err != nil {
				t.Fatal(err)
			}
			defer resp.Body.Close()
			reader := bufio.NewReader(resp.Body)
			first, err := reader.ReadString('\n')
			if err != nil || first != "data: first\n" {
				t.Fatalf("first frame was not flushed: %q %v", first, err)
			}
			rest, err := io.ReadAll(reader)
			if disable && (err != nil || !strings.Contains(string(rest), "data: second")) {
				t.Fatalf("cleared deadline truncated stream: %q %v", rest, err)
			}
			if !disable && (err == nil || strings.Contains(string(rest), "data: second")) {
				t.Fatalf("negative control did not expire: %q %v", rest, err)
			}
		})
	}
}

func TestSSEWriteDeadlineDoesNotCommitHeaders(t *testing.T) {
	c, w := searchbotContext(`{}`)
	disableWriteDeadlineForSSE(c) // Unsupported recorder is also usable.
	if c.Writer.Written() || w.Header().Get("Content-Type") != "" {
		t.Fatal("deadline helper committed a stream before authorization")
	}
}
