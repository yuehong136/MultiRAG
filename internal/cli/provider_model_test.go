package cli

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strconv"
	"testing"
	"time"
)

func TestProviderInstanceGrammar(t *testing.T) {
	for _, input := range []string{`CREATE PROVIDER 'vllm' INSTANCE 'local' KEY '' URL 'http://localhost:8000/v1' REGION 'fixture';`, `CREATE PROVIDER 'vllm' INSTANCE 'local' '' URL 'http://localhost:8000/v1' REGION 'fixture';`} {
		cmd, err := NewParser(input).Parse(false)
		if err != nil || cmd.Params["base_url"] != "http://localhost:8000/v1" || cmd.Params["region"] != "fixture" {
			t.Fatalf("%v %v", cmd, err)
		}
	}
	cmd, err := NewParser(`CREATE PROVIDER 'vllm' INSTANCE 'local' KEY 'key' URL 'http://localhost:8000/v1';`).Parse(false)
	if err != nil || cmd.Params["region"] != "local" {
		t.Fatalf("%v %v", cmd, err)
	}
	for _, input := range []string{`CREATE PROVIDER 'vllm' INSTANCE 'local' KEY 'key' URL 'x' URL 'y';`, `CREATE PROVIDER 'vllm' INSTANCE 'local' KEY 'key' REGION 'x' trailing;`} {
		if _, err := NewParser(input).Parse(false); err == nil {
			t.Fatalf("accepted %s", input)
		}
	}
}
func TestProviderInstanceEmptyURLAndRegion(t *testing.T) {
	cmd, err := NewParser(`CREATE PROVIDER 'vllm' INSTANCE 'local' KEY 'key' URL 'http://localhost:8000/v1' REGION '';`).Parse(false)
	if err != nil || cmd.Params["region"] != "local" {
		t.Fatalf("empty region %v %v", cmd, err)
	}
	cmd, err = NewParser(`CREATE PROVIDER 'vllm' INSTANCE 'local' KEY 'key' URL '';`).Parse(false)
	if err != nil || cmd.Params["region"] != nil {
		t.Fatalf("empty URL %v %v", cmd, err)
	}
}
func TestLegacyProviderInstanceWithoutSemicolon(t *testing.T) {
	if _, err := NewParser(`CREATE PROVIDER 'vllm' INSTANCE 'legacy' 'key'`).Parse(false); err != nil {
		t.Fatal(err)
	}
}
func TestCustomModelGrammarAndCapabilities(t *testing.T) {
	for option, typ := range map[string]string{"chat think": "chat", "vision think": "image2text", "asr": "speech2text", "ocr": "ocr", "embedding": "embedding", "rerank": "rerank", "tts": "tts"} {
		cmd, err := NewParser(`ADD MODEL 'Qwen/Qwen2-0.5B' TO PROVIDER 'vllm' INSTANCE 'local' WITH TOKENS 131072 ` + option + `;`).Parse(false)
		if err != nil || cmd.Type != "add_custom_model" || cmd.Params["model_type"] != typ || cmd.Params["max_tokens"] != 131072 {
			t.Fatalf("%s: %v %v", option, cmd, err)
		}
	}
	for _, option := range []string{"tokens 1 chat chat", "tokens 1 embedding think", "tokens 1 tokens 2 chat", "chat", "tokens 0 chat", "tokens 1.5 chat", "tokens 1 chat garbage", "tokens 1 chat think think"} {
		if _, err := NewParser(`ADD MODEL 'm' TO PROVIDER 'vllm' INSTANCE 'i' WITH ` + option + `;`).Parse(false); err == nil {
			t.Fatalf("accepted %s", option)
		}
	}
}
func TestCustomModelClientPayloadAndBusinessError(t *testing.T) {
	fail := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" || r.URL.Path != "/api/v1/providers/vllm/instances/local/models" {
			t.Errorf("%s %s", r.Method, r.URL.Path)
		}
		var payload map[string]interface{}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Error(err)
		}
		if payload["max_tokens"] != float64(1024) || payload["thinking"] != true || payload["model_name"] != "Qwen/test" || payload["model_type"] != "chat" || !reflect.DeepEqual(payload["model_types"], []interface{}{"chat", "image2text"}) {
			t.Errorf("%v", payload)
		}
		if fail {
			w.Write([]byte(`{"code":109,"message":"duplicate"}`))
		} else {
			w.Write([]byte(`{"code":0}`))
		}
	}))
	defer server.Close()
	u, _ := url.Parse(server.URL)
	port, _ := strconv.Atoi(u.Port())
	client := NewMultiRAGClient("user")
	client.HTTPClient.Host = u.Hostname()
	client.HTTPClient.Port = port
	client.HTTPClient.LoginToken = "fixture"
	cmd, err := NewParser(`ADD MODEL 'Qwen/test' TO PROVIDER 'vllm' INSTANCE 'local' WITH TOKENS 1024 CHAT VISION THINK;`).Parse(false)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.ExecuteUserCommand(cmd); err != nil {
		t.Fatal(err)
	}
	fail = true
	if _, err := client.ExecuteUserCommand(cmd); err == nil {
		t.Fatal("business error reported as success")
	}
	if client.HTTPClient.client.Timeout != 300*time.Second || client.HTTPClient.ReadTimeout != 300*time.Second {
		t.Fatal("inconsistent request timeout")
	}
}
