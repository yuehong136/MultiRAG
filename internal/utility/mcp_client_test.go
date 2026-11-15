package utility

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"multirag/internal/testsupport"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func mcpTestServer(t *testing.T, handler http.Handler) *httptest.Server {
	return testsupport.NewHTTP(t, handler)
}
func canonicalMCPClient(t *testing.T, server *httptest.Server) *http.Client {
	t.Helper()
	previous := LookupHost
	LookupHost = func(context.Context, string) ([]string, error) { return []string{"8.8.8.8"}, nil }
	t.Cleanup(func() { LookupHost = previous })
	client := &http.Client{Timeout: time.Second, Transport: &http.Transport{Proxy: nil, DialContext: func(ctx context.Context, network, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, network, server.Listener.Addr().String())
	}}}
	t.Cleanup(client.CloseIdleConnections)
	return client
}
func TestMCPDiscoveryCanonicalHTTPPaginationSession(t *testing.T) {
	var lists, deletes atomic.Int32
	server := mcpTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer fixture" {
			t.Error("credentials lost")
		}
		if r.Method == http.MethodDelete {
			deletes.Add(1)
			w.WriteHeader(204)
			return
		}
		var request jsonRPCRequest
		if json.NewDecoder(r.Body).Decode(&request) != nil {
			t.Error("bad request")
			w.WriteHeader(400)
			return
		}
		var result any
		switch request.Method {
		case "initialize":
			w.Header().Set(sessionHeader, "fixture-session")
			result = map[string]any{"protocolVersion": protocolVersion, "capabilities": map[string]any{}, "serverInfo": map[string]any{"name": "fixture", "version": "1"}}
		case "notifications/initialized":
			w.WriteHeader(202)
			return
		case "tools/list":
			if r.Header.Get(sessionHeader) != "fixture-session" || r.Header.Get("Mcp-Protocol-Version") != protocolVersion {
				t.Error("negotiated headers lost")
			}
			page := lists.Add(1)
			result = map[string]any{"tools": []any{map[string]any{"name": fmt.Sprint(page), "inputSchema": map[string]any{"type": "object"}, "extra": "kept"}}}
			if page == 1 {
				result.(map[string]any)["nextCursor"] = "second"
			} else if request.Params.(map[string]any)["cursor"] != "second" {
				t.Error("cursor lost")
			}
		default:
			t.Error("unexpected method", request.Method)
			w.WriteHeader(400)
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": request.ID, "result": result})
	}))
	tools, err := FetchTools(t.Context(), FetchOptions{URL: "http://mcp.invalid/rpc", ServerType: TransportStreamableHTTP, HTTPClient: canonicalMCPClient(t, server), Headers: map[string]string{"Authorization": "Bearer ${token}"}, Variables: map[string]string{"token": "fixture"}})
	if err != nil || len(tools) != 2 || lists.Load() != 2 || deletes.Load() != 1 || tools[0].Raw["extra"] != "kept" {
		t.Fatal(tools, err, lists.Load(), deletes.Load())
	}
}
func TestMCPDiscoveryCanonicalMalformedResultsAndBounds(t *testing.T) {
	for _, raw := range []string{`{}`, `null`, `{"tools":null}`, `{"tools":[{}]}`, `{"tools":[{"name":"x","inputSchema":null}]}`, `{"tools":[{"name":"valid","inputSchema":{}},{"name":1}]}`} {
		if result, err := parseToolsResult([]byte(raw)); err == nil || result != nil {
			t.Fatalf("partial/invalid tool success: %s", raw)
		}
	}
	for _, raw := range []string{`{}`, `null`, `{"jsonrpc":"2.0","result":{}}`, `{"jsonrpc":"1.0","id":0,"result":{}}`, `{"jsonrpc":"2.0","id":1,"result":{}}`, `{"jsonrpc":"2.0","id":0,"result":{},"error":{"code":1}}`, `{"jsonrpc":"2.0","id":0,"result":{}} {}`} {
		if _, err := parseJSONRPC([]byte(raw), 0); err == nil {
			t.Fatalf("invalid envelope accepted: %s", raw)
		}
	}
	for _, headers := range []map[string]string{{"Host": "other"}, {"Connection": "close"}, {"Mcp-Session-Id": "session"}, {"X-Token": "${token}"}} {
		if _, err := renderHeaders(headers, map[string]string{"token": "a\r\nX-Leak: value"}); err == nil {
			t.Fatal("unsafe header accepted")
		}
	}
	server := mcpTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, strings.Repeat(" ", (8<<20)+1)) }))
	if _, err := fetchToolsStreamableHTTP(t.Context(), server.URL, nil, server.Client()); err == nil {
		t.Fatal("oversized response accepted")
	}
	if _, err := readJSONRPCFromSSE(strings.NewReader("data: broken-json\n\n"), 0); err == nil {
		t.Fatal("malformed SSE hidden")
	}
}
func TestMCPDiscoveryCanonicalCancellationAndCrossOrigin(t *testing.T) {
	started := make(chan struct{})
	server := mcpTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		close(started)
		<-r.Context().Done()
	}))
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() { _, err := fetchToolsStreamableHTTP(ctx, server.URL, nil, server.Client()); done <- err }()
	<-started
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("MCP did not cancel")
	}
	sse := mcpTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "event: endpoint\ndata: http://other.invalid/message\n\n")
	}))
	if _, err := fetchToolsSSE(t.Context(), sse.URL, map[string]string{"Authorization": "fixture"}, sse.Client()); err == nil {
		t.Fatal("cross-origin SSE endpoint accepted")
	}
}

func TestMCPDiscoveryCanonicalSSELifecycle(t *testing.T) {
	responses := make(chan []byte, 4)
	server := mcpTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Fixture") != "credential" {
			t.Error("SSE credentials lost")
		}
		if r.Method == http.MethodGet {
			w.Header().Set("Content-Type", "text/event-stream")
			fmt.Fprint(w, "event: endpoint\ndata: /message\n\n")
			w.(http.Flusher).Flush()
			for {
				select {
				case payload := <-responses:
					fmt.Fprintf(w, "event: message\ndata: %s\n\n", payload)
					w.(http.Flusher).Flush()
				case <-r.Context().Done():
					return
				}
			}
		}
		var request jsonRPCRequest
		if json.NewDecoder(r.Body).Decode(&request) != nil {
			t.Error("invalid SSE POST")
			w.WriteHeader(400)
			return
		}
		var result any
		switch request.Method {
		case "initialize":
			result = map[string]any{"protocolVersion": protocolVersion, "capabilities": map[string]any{}, "serverInfo": map[string]any{"name": "fixture", "version": "1"}}
		case "notifications/initialized":
			w.WriteHeader(202)
			return
		case "tools/list":
			result = map[string]any{"tools": []any{map[string]any{"name": "sse-tool", "inputSchema": map[string]any{"type": "object"}}}}
		default:
			t.Error("unexpected SSE method")
			w.WriteHeader(400)
			return
		}
		payload, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": request.ID, "result": result})
		responses <- payload
		w.WriteHeader(202)
	}))
	tools, err := FetchTools(t.Context(), FetchOptions{URL: "http://mcp.invalid/sse", ServerType: TransportSSE, HTTPClient: canonicalMCPClient(t, server), Headers: map[string]string{"X-Fixture": "credential"}})
	if err != nil || len(tools) != 1 || tools[0].Name != "sse-tool" {
		t.Fatal(tools, err)
	}
}
