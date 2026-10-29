package utility

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net/http"
	"net/textproto"
	"net/url"
	"strconv"
	"strings"
	"time"
)

const (
	TransportSSE            = "sse"
	TransportStreamableHTTP = "streamable-http"
	mcpProtocolVersion      = "2025-03-26"
	mcpMaxResponse          = 1 << 20
	mcpMaxStream            = 8 << 20
	mcpMaxPages             = 32
	mcpMaxTools             = 2000
)

var (
	ErrMCPProtocol   = errors.New("Invalid MCP discovery response.")
	ErrMCPConnection = errors.New("MCP connection failed; check the endpoint and authentication settings.")
)

// Tool preserves the advertised descriptor for callers that cache tool metadata.
type Tool struct {
	Name        string         `json:"name"`
	Description string         `json:"description,omitempty"`
	InputSchema map[string]any `json:"inputSchema"`
	Raw         map[string]any `json:"-"`
}

// FetchOptions describes discovery only. No command or tool execution is supported.
type FetchOptions struct {
	URL        string
	ServerType string
	Headers    map[string]string
	Variables  map[string]string
	Timeout    time.Duration
}

// FetchTools owns the HTTP transport and closes all session and response state.
// There is deliberately no injectable public client that bypasses URL safety.
func FetchTools(ctx context.Context, opts FetchOptions) ([]Tool, error) {
	if opts.Timeout <= 0 {
		opts.Timeout = 10 * time.Second
	}
	if opts.Timeout > 60*time.Second {
		opts.Timeout = 60 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, opts.Timeout)
	defer cancel()
	if opts.ServerType != TransportSSE && opts.ServerType != TransportStreamableHTTP {
		return nil, errors.New("Unsupported MCP server type.")
	}
	host, ip, err := AssertURLSafe(ctx, opts.URL)
	if err != nil {
		return nil, err
	}
	client := PinnedHTTPClient(host, ip, opts.Timeout)
	defer client.CloseIdleConnections()
	tools, err := fetchMCPTools(ctx, opts, client)
	if err != nil && ctx.Err() != nil {
		return nil, ctx.Err()
	}
	return tools, err
}

func renderMCPHeaders(raw, variables map[string]string) (http.Header, error) {
	headers := http.Header{}
	if len(raw) > 64 {
		return nil, errors.New("Invalid MCP headers.")
	}
	size := 0
	for key, value := range raw {
		key, value = substituteMCPTemplate(key, variables), substituteMCPTemplate(value, variables)
		size += len(key) + len(value)
		if key == "" || len(key) > 128 || size > 16<<10 || strings.ContainsAny(value, "\r\n\x00") {
			return nil, errors.New("Invalid MCP headers.")
		}
		for _, r := range key {
			if r > 127 || !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || strings.ContainsRune("!#$%&'*+-.^_`|~", r)) {
				return nil, errors.New("Invalid MCP headers.")
			}
		}
		canonical := textproto.CanonicalMIMEHeaderKey(key)
		switch canonical {
		case "Host", "Connection", "Proxy-Authorization", "Proxy-Connection", "Transfer-Encoding", "Content-Length", "Content-Type", "Accept", "Mcp-Session-Id", "Mcp-Protocol-Version", "Upgrade", "Te", "Trailer":
			return nil, errors.New("Invalid MCP headers.")
		}
		if _, exists := headers[canonical]; exists {
			return nil, errors.New("Invalid MCP headers.")
		}
		headers.Set(canonical, value)
	}
	return headers, nil
}

// Supports Python Template's $name, ${name} and $$ forms without interpreting
// unknown variables or evaluating user supplied text.
func substituteMCPTemplate(value string, vars map[string]string) string {
	var out strings.Builder
	for i := 0; i < len(value); {
		if value[i] != '$' || i+1 == len(value) {
			out.WriteByte(value[i])
			i++
			continue
		}
		start := i
		i++
		if value[i] == '$' {
			out.WriteByte('$')
			i++
			continue
		}
		braced := value[i] == '{'
		if braced {
			i++
		}
		begin := i
		for i < len(value) && (value[i] == '_' || value[i] >= 'a' && value[i] <= 'z' || value[i] >= 'A' && value[i] <= 'Z' || i > begin && value[i] >= '0' && value[i] <= '9') {
			i++
		}
		key := value[begin:i]
		if key == "" || braced && (i == len(value) || value[i] != '}') {
			out.WriteString(value[start:i])
			continue
		}
		if braced {
			i++
		}
		if replacement, ok := vars[key]; ok {
			out.WriteString(replacement)
		} else {
			out.WriteString(value[start:i])
		}
	}
	return out.String()
}

type mcpRequest struct {
	JSONRPC string `json:"jsonrpc"`
	ID      *int   `json:"id,omitempty"`
	Method  string `json:"method"`
	Params  any    `json:"params,omitempty"`
}

type mcpWireResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Method  string          `json:"method"`
	Result  json.RawMessage `json:"result"`
	Error   json.RawMessage `json:"error"`
}

func parseMCPResponse(body []byte, id int) (json.RawMessage, bool, error) {
	var response mcpWireResponse
	decoder := json.NewDecoder(bytes.NewReader(body))
	if decoder.Decode(&response) != nil || response.JSONRPC != "2.0" {
		return nil, false, ErrMCPProtocol
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		return nil, false, ErrMCPProtocol
	}
	if response.Method != "" && len(response.ID) == 0 && len(response.Result) == 0 && len(response.Error) == 0 {
		return nil, true, nil
	}
	if response.Method != "" || string(response.ID) != strconv.Itoa(id) || len(response.Error) != 0 || len(response.Result) == 0 || string(response.Result) == "null" {
		return nil, false, ErrMCPProtocol
	}
	var result map[string]json.RawMessage
	if json.Unmarshal(response.Result, &result) != nil || result == nil {
		return nil, false, ErrMCPProtocol
	}
	return response.Result, false, nil
}

func fetchMCPTools(ctx context.Context, opts FetchOptions, client *http.Client) ([]Tool, error) {
	headers, err := renderMCPHeaders(opts.Headers, opts.Variables)
	if err != nil {
		return nil, err
	}
	var call func(context.Context, mcpRequest) (json.RawMessage, error)
	var sessionID string
	var protocol string
	if opts.ServerType == TransportStreamableHTTP {
		call = func(ctx context.Context, request mcpRequest) (json.RawMessage, error) {
			result, session, err := mcpStreamableSend(ctx, client, opts.URL, sessionID, protocol, headers, request)
			if session != "" {
				if sessionID != "" && sessionID != session {
					return nil, ErrMCPProtocol
				}
				sessionID = session
			}
			return result, err
		}
		defer func() {
			if sessionID != "" {
				// Request cancellation still releases a negotiated remote session;
				// this bounded close cannot keep the parent request alive indefinitely.
				closeCtx, cancel := context.WithTimeout(context.Background(), time.Second)
				defer cancel()
				req, _ := http.NewRequestWithContext(closeCtx, http.MethodDelete, opts.URL, nil)
				req.Header = headers.Clone()
				req.Header.Set("Mcp-Session-Id", sessionID)
				if protocol != "" {
					req.Header.Set("MCP-Protocol-Version", protocol)
				}
				if res, err := client.Do(req); err == nil {
					res.Body.Close()
				}
			}
		}()
	} else if opts.ServerType == TransportSSE {
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, opts.URL, nil)
		if err != nil {
			return nil, ErrMCPConnection
		}
		request.Header = headers.Clone()
		request.Header.Set("Accept", "text/event-stream")
		res, err := client.Do(request)
		if err != nil {
			return nil, mcpConnectionError(ctx)
		}
		defer res.Body.Close()
		media, _, _ := mime.ParseMediaType(res.Header.Get("Content-Type"))
		if res.StatusCode != http.StatusOK || media != "text/event-stream" {
			return nil, ErrMCPConnection
		}
		stream := newMCPSSEReader(res.Body)
		data, err := readMCPSSEEndpoint(stream)
		if err != nil {
			return nil, ErrMCPProtocol
		}
		base, _ := url.Parse(opts.URL)
		target, err := url.Parse(string(data))
		if err != nil {
			return nil, ErrMCPProtocol
		}
		target = base.ResolveReference(target)
		if target.User != nil || target.Fragment != "" || !sameMCPOrigin(base, target) {
			return nil, ErrMCPProtocol
		}
		call = func(ctx context.Context, request mcpRequest) (json.RawMessage, error) {
			body, _ := json.Marshal(request)
			req, err := http.NewRequestWithContext(ctx, http.MethodPost, target.String(), bytes.NewReader(body))
			if err != nil {
				return nil, ErrMCPConnection
			}
			req.Header = headers.Clone()
			req.Header.Set("Content-Type", "application/json")
			response, err := client.Do(req)
			if err != nil {
				return nil, mcpConnectionError(ctx)
			}
			_, readErr := readMCPBody(response.Body)
			response.Body.Close()
			if response.StatusCode < 200 || response.StatusCode >= 300 || readErr != nil {
				return nil, ErrMCPConnection
			}
			if request.ID == nil {
				return nil, nil
			}
			return readMCPSSEReply(stream, *request.ID)
		}
	} else {
		return nil, errors.New("Unsupported MCP server type.")
	}
	id := 0
	initial, err := call(ctx, mcpRequest{JSONRPC: "2.0", ID: &id, Method: "initialize", Params: map[string]any{"protocolVersion": mcpProtocolVersion, "capabilities": map[string]any{}, "clientInfo": map[string]any{"name": "multirag-go", "version": "1.0.0"}}})
	if err != nil {
		return nil, err
	}
	var init struct {
		ProtocolVersion string         `json:"protocolVersion"`
		Capabilities    map[string]any `json:"capabilities"`
		ServerInfo      map[string]any `json:"serverInfo"`
	}
	if json.Unmarshal(initial, &init) != nil || init.Capabilities == nil || init.ServerInfo == nil {
		return nil, ErrMCPProtocol
	}
	serverName, validName := init.ServerInfo["name"].(string)
	serverVersion, validVersion := init.ServerInfo["version"].(string)
	if !validName || !validVersion || strings.TrimSpace(serverName) == "" || strings.TrimSpace(serverVersion) == "" {
		return nil, ErrMCPProtocol
	}
	switch init.ProtocolVersion {
	case "2024-11-05", "2025-03-26", "2025-06-18":
		protocol = init.ProtocolVersion
	default:
		return nil, ErrMCPProtocol
	}
	if _, err = call(ctx, mcpRequest{JSONRPC: "2.0", Method: "notifications/initialized"}); err != nil {
		return nil, err
	}
	tools := []Tool{}
	seen := map[string]bool{}
	cursors := map[string]bool{}
	cursor := ""
	for page := 0; page < mcpMaxPages; page++ {
		id++
		params := map[string]any{}
		if cursor != "" {
			params["cursor"] = cursor
		}
		raw, err := call(ctx, mcpRequest{JSONRPC: "2.0", ID: &id, Method: "tools/list", Params: params})
		if err != nil {
			return nil, err
		}
		var result struct {
			Tools      []map[string]any `json:"tools"`
			NextCursor json.RawMessage  `json:"nextCursor"`
		}
		if json.Unmarshal(raw, &result) != nil || result.Tools == nil {
			return nil, ErrMCPProtocol
		}
		for _, descriptor := range result.Tools {
			name, ok := descriptor["name"].(string)
			schema, schemaOK := descriptor["inputSchema"].(map[string]any)
			if !ok || name == "" || len(name) > 128 || strings.TrimSpace(name) != name || seen[name] || !schemaOK || schema["type"] != "object" {
				return nil, ErrMCPProtocol
			}
			description := ""
			if value, exists := descriptor["description"]; exists {
				description, ok = value.(string)
				if !ok {
					return nil, ErrMCPProtocol
				}
			}
			seen[name] = true
			tools = append(tools, Tool{Name: name, Description: description, InputSchema: schema, Raw: descriptor})
			if len(tools) > mcpMaxTools {
				return nil, ErrMCPProtocol
			}
		}
		if len(result.NextCursor) == 0 {
			return tools, nil
		}
		if json.Unmarshal(result.NextCursor, &cursor) != nil || cursor == "" || len(cursor) > 4096 || cursors[cursor] {
			return nil, ErrMCPProtocol
		}
		cursors[cursor] = true
	}
	return nil, ErrMCPProtocol
}

func sameMCPOrigin(left, right *url.URL) bool {
	port := func(u *url.URL) string {
		if u.Port() != "" {
			return u.Port()
		}
		if u.Scheme == "https" {
			return "443"
		}
		return "80"
	}
	return left.Scheme == right.Scheme && strings.EqualFold(left.Hostname(), right.Hostname()) && port(left) == port(right)
}

func mcpConnectionError(ctx context.Context) error {
	if ctx.Err() != nil {
		return ctx.Err()
	}
	return ErrMCPConnection
}

func readMCPBody(body io.Reader) ([]byte, error) {
	data, err := io.ReadAll(io.LimitReader(body, mcpMaxResponse+1))
	if err != nil || len(data) > mcpMaxResponse {
		return nil, ErrMCPProtocol
	}
	return data, nil
}

func mcpStreamableSend(ctx context.Context, client *http.Client, endpoint, session, protocol string, headers http.Header, payload mcpRequest) (json.RawMessage, string, error) {
	body, _ := json.Marshal(payload)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return nil, "", ErrMCPConnection
	}
	req.Header = headers.Clone()
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json, text/event-stream")
	if session != "" {
		req.Header.Set("Mcp-Session-Id", session)
	}
	if protocol != "" {
		req.Header.Set("MCP-Protocol-Version", protocol)
	}
	res, err := client.Do(req)
	if err != nil {
		return nil, "", mcpConnectionError(ctx)
	}
	defer res.Body.Close()
	newSession := res.Header.Get("Mcp-Session-Id")
	if len(newSession) > 256 {
		return nil, "", ErrMCPProtocol
	}
	for _, r := range newSession {
		if r < 33 || r > 126 {
			return nil, "", ErrMCPProtocol
		}
	}
	if res.StatusCode < 200 || res.StatusCode >= 300 {
		return nil, newSession, ErrMCPConnection
	}
	if payload.ID == nil {
		_, err := readMCPBody(res.Body)
		return nil, newSession, err
	}
	media, _, _ := mime.ParseMediaType(res.Header.Get("Content-Type"))
	if media == "text/event-stream" {
		result, err := readMCPSSEReply(newMCPSSEReader(res.Body), *payload.ID)
		return result, newSession, err
	}
	if media != "application/json" {
		return nil, newSession, ErrMCPProtocol
	}
	data, err := readMCPBody(res.Body)
	if err != nil {
		return nil, newSession, err
	}
	result, notification, err := parseMCPResponse(data, *payload.ID)
	if notification {
		return nil, newSession, ErrMCPProtocol
	}
	return result, newSession, err
}

// The scanner limits each line; total wire bytes and each complete event also
// have explicit bounds, including comment-only or notification-only streams.
type mcpSSEReader struct {
	scanner *bufio.Scanner
	bytes   int
}

func newMCPSSEReader(reader io.Reader) *mcpSSEReader {
	scanner := bufio.NewScanner(io.LimitReader(reader, mcpMaxStream+1))
	scanner.Buffer(make([]byte, 4096), mcpMaxResponse)
	return &mcpSSEReader{scanner: scanner}
}
func (s *mcpSSEReader) next() (string, []byte, error) {
	event := ""
	data := []byte{}
	for s.scanner.Scan() {
		line := s.scanner.Text()
		s.bytes += len(line) + 1
		if s.bytes > mcpMaxStream {
			return "", nil, ErrMCPProtocol
		}
		if line == "" {
			if len(data) > 0 || event != "" {
				return event, bytes.TrimSuffix(data, []byte("\n")), nil
			}
			continue
		}
		if strings.HasPrefix(line, ":") {
			continue
		}
		field, value, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		value = strings.TrimPrefix(value, " ")
		switch field {
		case "event":
			event = value
		case "data":
			data = append(data, value...)
			data = append(data, '\n')
			if len(data) > mcpMaxResponse {
				return "", nil, ErrMCPProtocol
			}
		}
	}
	return "", nil, ErrMCPProtocol
}

// Named heartbeat events may precede the endpoint. Only protocol messages are
// parsed; their malformed envelopes must not be hidden as keepalive traffic.
func readMCPSSEEndpoint(stream *mcpSSEReader) ([]byte, error) {
	for count := 0; count < 128; count++ {
		event, data, err := stream.next()
		if err != nil {
			return nil, err
		}
		if event == "endpoint" {
			if len(data) == 0 {
				return nil, ErrMCPProtocol
			}
			return data, nil
		}
		if event == "" || event == "message" {
			if len(data) == 0 {
				continue
			}
			_, notification, err := parseMCPResponse(data, 0)
			if err != nil || !notification {
				return nil, ErrMCPProtocol
			}
		}
	}
	return nil, ErrMCPProtocol
}

func readMCPSSEReply(stream *mcpSSEReader, id int) (json.RawMessage, error) {
	for count := 0; count < 128; count++ {
		event, data, err := stream.next()
		if err != nil {
			return nil, err
		}
		if event != "" && event != "message" || len(data) == 0 {
			continue
		}
		result, notification, err := parseMCPResponse(data, id)
		if err != nil {
			return nil, err
		}
		if notification {
			continue
		}
		return result, nil
	}
	return nil, ErrMCPProtocol
}
