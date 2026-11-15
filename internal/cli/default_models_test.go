package cli

import (
	"encoding/json"
	"net/http"
	"net/url"
	"strconv"
	"testing"
)

func TestParseSetDefaultModel(t *testing.T) {
	cmd, err := NewParser(`SET DEFAULT VISION MODEL "glm-4.6v-flash@ccc@zhipu-ai";`).Parse(APIMode)
	if err != nil {
		t.Fatalf("Parse() error = %v", err)
	}

	want := map[string]interface{}{
		"model_type":           "vision",
		"composite_model_name": "glm-4.6v-flash@ccc@zhipu-ai",
	}
	if cmd.Type != "set_default_model" {
		t.Fatalf("command type = %q, want set_default_model", cmd.Type)
	}
	for key, value := range want {
		if cmd.Params[key] != value {
			t.Errorf("%s = %#v, want %#v", key, cmd.Params[key], value)
		}
	}
}

func TestDefaultModelCommandsUseModelsEndpoint(t *testing.T) {
	var requests []struct {
		method string
		body   map[string]interface{}
	}
	server := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != map[string]string{http.MethodGet: "/api/v1/models/default", http.MethodPatch: "/api/v1/models"}[r.Method] {
			t.Errorf("path = %q, want /api/v1/models/default", r.URL.Path)
		}
		entry := struct {
			method string
			body   map[string]interface{}
		}{method: r.Method}
		if r.Body != nil {
			_ = json.NewDecoder(r.Body).Decode(&entry.body)
		}
		requests = append(requests, entry)

		w.Header().Set("Content-Type", "application/json")
		if r.Method == http.MethodGet {
			_, _ = w.Write([]byte(`{"code":0,"data":{"models":[{"model_type":"llm"}]},"message":"success"}`))
			return
		}
		_, _ = w.Write([]byte(`{"code":0,"message":"success"}`))
	}))
	defer server.Close()

	serverURL, err := url.Parse(server.URL)
	if err != nil {
		t.Fatalf("url.Parse() error = %v", err)
	}
	port, err := strconv.Atoi(serverURL.Port())
	if err != nil {
		t.Fatalf("strconv.Atoi() error = %v", err)
	}
	client := newConsumerCLI("user")
	consumerHTTP(client).Host = serverURL.Hostname()
	consumerHTTP(client).Port = port

	listCmd, err := NewParser("LIST DEFAULT MODELS;").Parse(APIMode)
	if err != nil {
		t.Fatalf("parse list command: %v", err)
	}
	if _, err = client.ExecuteUserCommand(listCmd); err != nil {
		t.Fatalf("execute list command: %v", err)
	}

	setCmd, err := NewParser(`SET DEFAULT CHAT MODEL "glm-4.5@default@zhipu-ai";`).Parse(APIMode)
	if err != nil {
		t.Fatalf("parse set command: %v", err)
	}
	if _, err = client.ExecuteUserCommand(setCmd); err != nil {
		t.Fatalf("execute set command: %v", err)
	}

	if len(requests) != 2 {
		t.Fatalf("request count = %d, want 2", len(requests))
	}
	if requests[0].method != http.MethodGet {
		t.Errorf("list method = %q, want GET", requests[0].method)
	}
	if requests[1].method != http.MethodPatch {
		t.Errorf("set method = %q, want PATCH", requests[1].method)
	}
	if requests[1].body["model_type"] != "chat" {
		t.Errorf("model_type = %#v, want chat", requests[1].body["model_type"])
	}
}
