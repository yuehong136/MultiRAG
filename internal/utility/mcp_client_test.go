package utility

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

var mcpJournalMu sync.Mutex

func mcpResourceRecord(t *testing.T, record map[string]any) {
	t.Helper()
	path := os.Getenv("MULTIRAG_PROVIDER_TEST_JOURNAL")
	if path == "" {
		if os.Getenv("MULTIRAG_RUN_ID") != "" {
			t.Fatal("registered MCP run requires resource journal")
		}
		return
	}
	mcpJournalMu.Lock()
	defer mcpJournalMu.Unlock()
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

func mcpTestServer(t *testing.T, handler http.Handler) *httptest.Server {
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
		mcpResourceRecord(t, map[string]any{"action": "intent", "run_id": identity, "kind": "http-listener", "address": address})
		if conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond); err == nil {
			conn.Close()
			mcpResourceRecord(t, map[string]any{"action": "occupied-borrowed", "run_id": identity, "kind": "http-listener", "address": address})
			continue
		}
		var err error
		listener, err = net.Listen("tcp", address)
		if err == nil {
			break
		}
		mcpResourceRecord(t, map[string]any{"action": "allocation-unavailable", "run_id": identity, "kind": "http-listener", "address": address})
	}
	if listener == nil {
		t.Fatal("no unoccupied MCP test address")
	}
	address := listener.Addr().String()
	mcpResourceRecord(t, map[string]any{"action": "allocated", "run_id": identity, "kind": "http-listener", "address": address})
	server := &httptest.Server{Listener: listener, Config: &http.Server{Handler: handler}}
	t.Cleanup(func() {
		server.Close()
		conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
		absent := err != nil
		if conn != nil {
			conn.Close()
		}
		mcpResourceRecord(t, map[string]any{"action": "cleanup-readback", "run_id": identity, "kind": "http-listener", "address": address, "absent": absent})
		if !absent {
			t.Error("MCP listener remains", address)
		}
	})
	server.Start()
	return server
}

func mcpTestClient(t *testing.T) *http.Client {
	t.Helper()
	client := &http.Client{Timeout: time.Second, Transport: &http.Transport{Proxy: nil}, CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("redirect forbidden") }}
	t.Cleanup(client.CloseIdleConnections)
	return client
}

func mcpInitResult() map[string]any {
	return map[string]any{"protocolVersion": mcpProtocolVersion, "capabilities": map[string]any{"tools": map[string]any{}}, "serverInfo": map[string]any{"name": "fixture", "version": "1"}}
}
func mcpTool(name string) map[string]any {
	return map[string]any{"name": name, "description": "public descriptor", "inputSchema": map[string]any{"type": "object", "properties": map[string]any{}}}
}
func mcpReply(id *int, result any) map[string]any {
	return map[string]any{"jsonrpc": "2.0", "id": id, "result": result}
}

func TestMCPDiscoveryStreamableHTTPPaginationSession(t *testing.T) {
	var deletes atomic.Int32
	var lists atomic.Int32
	server := mcpTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer secret-value" {
			t.Error("rendered authorization missing")
		}
		if r.Method == http.MethodDelete {
			deletes.Add(1)
			if r.Header.Get("Mcp-Session-Id") != "fixture-session" {
				t.Error("session missing on close")
			}
			w.WriteHeader(204)
			return
		}
		var request mcpRequest
		if json.NewDecoder(r.Body).Decode(&request) != nil {
			t.Error("invalid client payload")
			w.WriteHeader(400)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		switch request.Method {
		case "initialize":
			w.Header().Set("Mcp-Session-Id", "fixture-session")
			json.NewEncoder(w).Encode(mcpReply(request.ID, mcpInitResult()))
		case "notifications/initialized":
			if request.ID != nil {
				t.Error("notification has id")
			}
			w.WriteHeader(202)
		case "tools/list":
			if r.Header.Get("Mcp-Session-Id") != "fixture-session" || r.Header.Get("Mcp-Protocol-Version") != mcpProtocolVersion {
				t.Error("negotiated headers missing")
			}
			if lists.Add(1) == 1 {
				json.NewEncoder(w).Encode(mcpReply(request.ID, map[string]any{"tools": []any{mcpTool("one")}, "nextCursor": "page-two"}))
			} else {
				params, _ := request.Params.(map[string]any)
				if params["cursor"] != "page-two" {
					t.Error("pagination cursor missing")
				}
				json.NewEncoder(w).Encode(mcpReply(request.ID, map[string]any{"tools": []any{mcpTool("two")}}))
			}
		default:
			t.Error("unexpected client method")
		}
	}))
	tools, err := fetchMCPTools(context.Background(), FetchOptions{URL: server.URL, ServerType: TransportStreamableHTTP, Headers: map[string]string{"Authorization": "Bearer ${token}"}, Variables: map[string]string{"token": "secret-value"}}, mcpTestClient(t))
	if err != nil || len(tools) != 2 || tools[0].Name != "one" || tools[1].Name != "two" || lists.Load() != 2 || deletes.Load() != 1 {
		t.Fatal("discovery/session failed", err, lists.Load(), deletes.Load())
	}
}

func TestMCPDiscoveryRejectsMalformedAndBoundedResponses(t *testing.T) {
	cases := []struct{ name, body string }{
		{"missing-id", `{"jsonrpc":"2.0","result":{"tools":[]}}`},
		{"null-id", `{"jsonrpc":"2.0","id":null,"result":{"tools":[]}}`},
		{"string-id", `{"jsonrpc":"2.0","id":"1","result":{"tools":[]}}`},
		{"wrong-id", `{"jsonrpc":"2.0","id":5,"result":{"tools":[]}}`},
		{"wrong-version", `{"id":1,"result":{"tools":[]}}`},
		{"missing-result", `{"jsonrpc":"2.0","id":1}`},
		{"empty-result", `{"jsonrpc":"2.0","id":1,"result":{}}`},
		{"null-tools", `{"jsonrpc":"2.0","id":1,"result":{"tools":null}}`},
		{"invalid-schema", `{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"one","inputSchema":{}}]}}`},
		{"duplicate-tool", `{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"one","inputSchema":{"type":"object"}},{"name":"one","inputSchema":{"type":"object"}}]}}`},
		{"null-cursor", `{"jsonrpc":"2.0","id":1,"result":{"tools":[],"nextCursor":null}}`},
		{"rpc-error", `{"jsonrpc":"2.0","id":1,"error":{"code":-1,"message":"secret-credential"}}`},
		{"trailing-json", `{"jsonrpc":"2.0","id":1,"result":{"tools":[]}} {}`},
		{"oversized", strings.Repeat("x", mcpMaxResponse+1)},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var closes atomic.Int32
			server := mcpTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method == http.MethodDelete {
					closes.Add(1)
					w.WriteHeader(204)
					return
				}
				var request mcpRequest
				json.NewDecoder(r.Body).Decode(&request)
				w.Header().Set("Content-Type", "application/json")
				switch request.Method {
				case "initialize":
					w.Header().Set("Mcp-Session-Id", "session")
					json.NewEncoder(w).Encode(mcpReply(request.ID, mcpInitResult()))
				case "notifications/initialized":
					w.WriteHeader(202)
				default:
					io.WriteString(w, tc.body)
				}
			}))
			_, err := fetchMCPTools(context.Background(), FetchOptions{URL: server.URL, ServerType: TransportStreamableHTTP}, mcpTestClient(t))
			if err == nil || strings.Contains(err.Error(), "secret-credential") || strings.Contains(err.Error(), server.URL) || closes.Load() != 1 {
				t.Fatal("invalid response accepted/leaked or session not closed", err, closes.Load())
			}
		})
	}
}

func TestMCPDiscoveryEmptyToolsAndCursorBound(t *testing.T) {
	for _, repeat := range []bool{false, true} {
		t.Run(fmt.Sprint(repeat), func(t *testing.T) {
			server := mcpTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method == http.MethodDelete {
					w.WriteHeader(204)
					return
				}
				var request mcpRequest
				json.NewDecoder(r.Body).Decode(&request)
				w.Header().Set("Content-Type", "application/json")
				switch request.Method {
				case "initialize":
					json.NewEncoder(w).Encode(mcpReply(request.ID, mcpInitResult()))
				case "notifications/initialized":
					w.WriteHeader(202)
				default:
					result := map[string]any{"tools": []any{}}
					if repeat {
						result["nextCursor"] = "repeated"
					}
					json.NewEncoder(w).Encode(mcpReply(request.ID, result))
				}
			}))
			tools, err := fetchMCPTools(context.Background(), FetchOptions{URL: server.URL, ServerType: TransportStreamableHTTP}, mcpTestClient(t))
			if repeat && err == nil || !repeat && (err != nil || tools == nil || len(tools) != 0) {
				t.Fatal("empty tools or cursor semantics", err)
			}
		})
	}
}

func TestMCPDiscoveryLegacySSELifecycle(t *testing.T) {
	messages := make(chan []byte, 4)
	closed := make(chan struct{})
	server := mcpTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Token") != "secret-value" {
			t.Error("legacy credential absent")
		}
		if r.Method == http.MethodGet {
			defer close(closed)
			w.Header().Set("Content-Type", "text/event-stream")
			fmt.Fprint(w, "event: heartbeat\ndata: alive\n\nevent: message\ndata:\n\ndata:\n\nevent: endpoint\ndata: /message\n\n")
			w.(http.Flusher).Flush()
			for {
				select {
				case message := <-messages:
					fmt.Fprintf(w, "event: heartbeat\ndata: alive\n\nevent: message\ndata:\n\ndata:\n\nevent: message\ndata: %s\n\n", message)
					w.(http.Flusher).Flush()
				case <-r.Context().Done():
					return
				}
			}
		}
		var request mcpRequest
		json.NewDecoder(r.Body).Decode(&request)
		switch request.Method {
		case "initialize":
			body, _ := json.Marshal(mcpReply(request.ID, mcpInitResult()))
			messages <- body
		case "tools/list":
			body, _ := json.Marshal(mcpReply(request.ID, map[string]any{"tools": []any{mcpTool("legacy")}}))
			messages <- body
		}
		w.WriteHeader(202)
	}))
	tools, err := fetchMCPTools(context.Background(), FetchOptions{URL: server.URL + "/sse", ServerType: TransportSSE, Headers: map[string]string{"X-Token": "secret-value"}}, mcpTestClient(t))
	if err != nil || len(tools) != 1 || tools[0].Name != "legacy" {
		t.Fatal("SSE discovery failed", err)
	}
	select {
	case <-closed:
	case <-time.After(time.Second):
		t.Fatal("legacy SSE reader/session remains")
	}
}

func TestMCPDiscoveryRejectsCrossOriginSSEBeforePosting(t *testing.T) {
	for _, target := range []string{"http://else.invalid/message", "https://127.0.0.1/message", "http://127.0.0.1:1/message", "http://user:credential@127.0.0.1/message"} {
		t.Run(target, func(t *testing.T) {
			var posts atomic.Int32
			server := mcpTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method == http.MethodPost {
					posts.Add(1)
					w.WriteHeader(202)
					return
				}
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprintf(w, "event: endpoint\ndata: %s\n\n", target)
			}))
			_, err := fetchMCPTools(context.Background(), FetchOptions{URL: server.URL, ServerType: TransportSSE, Headers: map[string]string{"X-Token": "secret-value"}}, mcpTestClient(t))
			if err == nil || posts.Load() != 0 || strings.Contains(err.Error(), "credential") {
				t.Fatal("unsafe endpoint accepted or credential leaked", err)
			}
		})
	}
}

func TestMCPDiscoveryInlineSSEAndCancellation(t *testing.T) {
	for _, cancelled := range []bool{false, true} {
		t.Run(fmt.Sprint(cancelled), func(t *testing.T) {
			server := mcpTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var request mcpRequest
				json.NewDecoder(r.Body).Decode(&request)
				if request.Method == "notifications/initialized" {
					w.WriteHeader(202)
					return
				}
				w.Header().Set("Content-Type", "text/event-stream")
				var result any = mcpInitResult()
				if request.Method == "tools/list" {
					if cancelled {
						w.WriteHeader(200)
						w.(http.Flusher).Flush()
						<-r.Context().Done()
						return
					}
					result = map[string]any{"tools": []any{mcpTool("inline")}}
				}
				response, _ := json.Marshal(mcpReply(request.ID, result))
				fmt.Fprintf(w, ": comment\n\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}\n\nevent: message\ndata: %s\n\n", response)
			}))
			ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
			defer cancel()
			start := time.Now()
			tools, err := fetchMCPTools(ctx, FetchOptions{URL: server.URL, ServerType: TransportStreamableHTTP}, mcpTestClient(t))
			if cancelled {
				if err == nil || time.Since(start) > time.Second {
					t.Fatal("cancelled stream not closed", err)
				}
			} else if err != nil || len(tools) != 1 {
				t.Fatal("inline SSE failed", err)
			}
		})
	}
}

func TestMCPDiscoveryProtocolFailureAndHeaderSafety(t *testing.T) {
	for _, body := range []string{`{}`, `{"jsonrpc":"2.0","result":{}}`, `{"jsonrpc":"2.0","id":0,"result":null}`, `{"jsonrpc":"2.0","id":0,"result":{},"error":null}`} {
		if _, _, err := parseMCPResponse([]byte(body), 0); err == nil {
			t.Fatal("bad init envelope accepted")
		}
	}
	for _, headers := range []map[string]string{{"Host": "other"}, {"X-Secret": "${token}"}, {"Mcp-Session-Id": "session"}, {"Connection": "close"}, {"${key}": "x"}, {"x-token": "a", "X-Token": "b"}} {
		_, err := renderMCPHeaders(headers, map[string]string{"token": "a\r\nX-Leak: value", "key": "bad\r\nkey"})
		if err == nil {
			t.Fatal("unsafe rendered headers accepted")
		}
	}
	if got := substituteMCPTemplate("$token ${token} $$ ${unknown}", map[string]string{"token": "value"}); got != "value value $ ${unknown}" {
		t.Fatal("template mismatch", got)
	}
	if _, err := FetchTools(context.Background(), FetchOptions{URL: "http://127.0.0.1", ServerType: "stdio"}); err == nil {
		t.Fatal("command transport accepted")
	}
}

func TestMCPDiscoverySSEHeartbeatBudgetAndMessageErrors(t *testing.T) {
	heartbeat := "event: heartbeat\ndata: alive\n\n"
	for _, count := range []int{127, 128} {
		_, err := readMCPSSEEndpoint(newMCPSSEReader(strings.NewReader(strings.Repeat(heartbeat, count) + "event: endpoint\ndata: /message\n\n")))
		if (err != nil) != (count == 128) {
			t.Fatal("endpoint event budget", count, err)
		}
		reply := `{"jsonrpc":"2.0","id":0,"result":{"tools":[]}}`
		_, err = readMCPSSEReply(newMCPSSEReader(strings.NewReader(strings.Repeat(heartbeat, count)+"event: message\ndata: "+reply+"\n\n")), 0)
		if (err != nil) != (count == 128) {
			t.Fatal("reply event budget", count, err)
		}
	}
	for _, event := range []string{"message", ""} {
		malformed := heartbeat + "event: " + event + "\ndata: invalid-json\n\nevent: endpoint\ndata: /message\n\n"
		if _, err := readMCPSSEEndpoint(newMCPSSEReader(strings.NewReader(malformed))); err == nil {
			t.Fatal("malformed protocol message hidden before endpoint")
		}
		if _, err := readMCPSSEReply(newMCPSSEReader(strings.NewReader(malformed)), 0); err == nil {
			t.Fatal("malformed protocol message hidden before response")
		}
	}
}

func TestMCPDiscoveryInitializeServerInfo(t *testing.T) {
	cases := []struct {
		name    string
		info    any
		missing bool
	}{
		{name: "missing", missing: true},
		{name: "null", info: nil},
		{name: "empty", info: map[string]any{}},
		{name: "missing-name", info: map[string]any{"version": "1"}},
		{name: "missing-version", info: map[string]any{"name": "fixture"}},
		{name: "numeric-name", info: map[string]any{"name": 1, "version": "1"}},
		{name: "numeric-version", info: map[string]any{"name": "fixture", "version": 1}},
		{name: "empty-name", info: map[string]any{"name": "", "version": "1"}},
		{name: "empty-version", info: map[string]any{"name": "fixture", "version": ""}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var requests atomic.Int32
			server := mcpTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				requests.Add(1)
				var request mcpRequest
				json.NewDecoder(r.Body).Decode(&request)
				result := mcpInitResult()
				if tc.missing {
					delete(result, "serverInfo")
				} else {
					result["serverInfo"] = tc.info
				}
				w.Header().Set("Content-Type", "application/json")
				json.NewEncoder(w).Encode(mcpReply(request.ID, result))
			}))
			_, err := fetchMCPTools(context.Background(), FetchOptions{URL: server.URL, ServerType: TransportStreamableHTTP}, mcpTestClient(t))
			if err == nil || requests.Load() != 1 {
				t.Fatal("invalid initialize result accepted or discovery continued", err, requests.Load())
			}
		})
	}
}

func TestMCPDiscoveryEmptySSEMessageBudget(t *testing.T) {
	for _, keepalive := range []string{"event: message\ndata:\n\n", "data:\n\n"} {
		for _, count := range []int{127, 128} {
			_, err := readMCPSSEEndpoint(newMCPSSEReader(strings.NewReader(strings.Repeat(keepalive, count) + "event: endpoint\ndata: /message\n\n")))
			if (err != nil) != (count == 128) {
				t.Fatal("empty endpoint message budget", count, err)
			}
			reply := `{"jsonrpc":"2.0","id":0,"result":{"tools":[]}}`
			_, err = readMCPSSEReply(newMCPSSEReader(strings.NewReader(strings.Repeat(keepalive, count)+"data: "+reply+"\n\n")), 0)
			if (err != nil) != (count == 128) {
				t.Fatal("empty reply message budget", count, err)
			}
		}
	}
}
