package cli

import (
	"encoding/json"
	"multirag/tests/support"
	"net/http"
	"net/url"
	"reflect"
	"strconv"
	"testing"
	"time"
)

func TestProviderInstanceGrammar(t *testing.T) {
	for _, input := range []string{`CREATE PROVIDER 'vllm' INSTANCE 'local' KEY '' URL 'http://localhost:8000/v1' REGION 'fixture';`} {
		cmd, err := NewParser(input).Parse(APIMode)
		if err != nil || cmd.Params["base_url"] != "http://localhost:8000/v1" || cmd.Params["region"] != "fixture" {
			t.Fatalf("%v %v", cmd, err)
		}
	}
	cmd, err := NewParser(`CREATE PROVIDER 'vllm' INSTANCE 'local' KEY 'key' URL 'http://localhost:8000/v1';`).Parse(APIMode)
	if err != nil || cmd.Params["region"] != nil {
		t.Fatalf("%v %v", cmd, err)
	}
	for _, input := range []string{`CREATE PROVIDER 'vllm' INSTANCE 'local' KEY 'key' URL 'x' URL 'y';`, `CREATE PROVIDER 'vllm' INSTANCE 'local' KEY 'key' REGION 'x' trailing;`} {
		if _, err := NewParser(input).Parse(APIMode); err == nil {
			t.Fatalf("accepted %s", input)
		}
	}
}
func TestProviderInstanceEmptyURLAndRegion(t *testing.T) {
	cmd, err := NewParser(`CREATE PROVIDER 'vllm' INSTANCE 'local' KEY 'key' URL 'http://localhost:8000/v1' REGION '';`).Parse(APIMode)
	if err != nil || cmd.Params["region"] != nil {
		t.Fatalf("empty region %v %v", cmd, err)
	}
	cmd, err = NewParser(`CREATE PROVIDER 'vllm' INSTANCE 'local' KEY 'key' URL '';`).Parse(APIMode)
	if err != nil || cmd.Params["region"] != nil {
		t.Fatalf("empty URL %v %v", cmd, err)
	}
}
func TestLegacyProviderInstanceWithoutSemicolon(t *testing.T) {
	if _, err := NewParser(`CREATE PROVIDER 'vllm' INSTANCE 'legacy' KEY 'key'`).Parse(APIMode); err != nil {
		t.Fatal(err)
	}
}
func TestCustomModelGrammarAndCapabilities(t *testing.T) {
	for option, typ := range map[string]string{"chat think": "chat", "vision think": "vision", "asr": "asr", "ocr": "ocr", "embedding": "embedding", "rerank": "rerank", "tts": "tts"} {
		cmd, err := NewParser(`ADD MODEL 'Qwen/Qwen2-0.5B' TO PROVIDER 'vllm' INSTANCE 'local' WITH TOKENS 131072 ` + option + `;`).Parse(APIMode)
		if err != nil || cmd.Type != "add_custom_model" || cmd.Params["models"].([]map[string]any)[0]["model_types"].([]string)[0] != typ || cmd.Params["models"].([]map[string]any)[0]["max_tokens"] != 131072 {
			t.Fatalf("%s: %v %v", option, cmd, err)
		}
	}
	for _, option := range []string{"tokens -1 chat", "tokens 1.5 chat", "tokens 1 chat garbage", "tokens 1 chat think think"} {
		if _, err := NewParser(`ADD MODEL 'm' TO PROVIDER 'vllm' INSTANCE 'i' WITH ` + option + `;`).Parse(APIMode); err == nil {
			t.Fatalf("accepted %s", option)
		}
	}
}
func TestCustomModelClientPayloadAndBusinessError(t *testing.T) {
	fail := false
	server := support.RegisteredHTTPServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" || r.URL.Path != "/api/v1/providers/vllm/instances/local/models" {
			t.Errorf("%s %s", r.Method, r.URL.Path)
		}
		var payload map[string]interface{}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Error(err)
		}
		models, ok := payload["models"].([]interface{})
		if !ok || len(models) != 1 {
			t.Fatalf("invalid batch: %v", payload)
		}
		model := models[0].(map[string]interface{})
		if model["max_tokens"] != float64(1024) || model["thinking"] != true || model["model_name"] != "Qwen/test" || !reflect.DeepEqual(model["model_types"], []interface{}{"chat", "vision"}) {
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
	client := newConsumerCLI("user")
	consumerHTTP(client).Host = u.Hostname()
	consumerHTTP(client).Port = port
	consumerHTTP(client).LoginToken = consumerTokenPtr("fixture")
	cmd, err := NewParser(`ADD MODEL 'Qwen/test' TO PROVIDER 'vllm' INSTANCE 'local' WITH TOKENS 1024 CHAT VISION THINK;`).Parse(APIMode)
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
	if consumerHTTP(client).client.Timeout != 300*time.Second || consumerHTTP(client).ReadTimeout != 60*time.Second {
		t.Fatal("inconsistent request timeout")
	}
}

func TestCreateDefaultProviderInstance(t *testing.T) {
	cmd, err := NewParser(`CREATE PROVIDER 'vllm' INSTANCE 'default' KEY '' URL 'http://localhost:8000/v1';`).Parse(APIMode)
	if err != nil || cmd.Params["instance_name"] != "default" {
		t.Fatalf("default instance: %+v %v", cmd, err)
	}
}
