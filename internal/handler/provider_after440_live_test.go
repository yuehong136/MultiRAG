package handler_test

import (
	"bytes"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/utility"
)

// An opt-in acceptance test for the Go provider line. The only external boundary
// controlled here is provider HTTP; routing, JWT, SQL, factories and CLI are real.
func TestAfter440ProvidersHTTPCLIAndSQL(t *testing.T) {
	config, binary := os.Getenv("MULTIRAG_AFTER440_D_CONFIG"), os.Getenv("MULTIRAG_AFTER440_D_CLI")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned D-line scratch config required")
		}
		t.Skip("requires owned D-line scratch database")
	}
	if binary == "" {
		t.Fatal("candidate CLI binary required")
	}
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal("load private config failed")
	}
	if server.GetConfig().Database.Database != "multirag_after440_d_20261025" {
		t.Fatal("unowned database")
	}
	if err := dao.InitDB(); err != nil {
		t.Fatal("initialize scratch", err)
	}
	pool, _ := dao.DB.DB()
	defer pool.Close()
	dsn, err := dao.PostgresDSN(server.GetConfig().Database)
	if err != nil {
		t.Fatal(err)
	}
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer readback.Close()
	if err := server.InitVariables(nil); err != nil {
		t.Fatal(err)
	}
	local.InitAdminStatus(0, "owned D-line acceptance")
	active := "1"
	tokens := map[string]string{}
	for _, id := range []string{"d-owner", "d-other"} {
		email := id + "@after440.invalid"
		if err := dao.DB.Create(&entity.User{ID: id, Email: email, Nickname: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := dao.DB.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: id + "-tenant", Role: "owner", InvitedBy: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		token, err := utility.SignUserJWT(email, server.GetSecretKey())
		if err != nil {
			t.Fatal(err)
		}
		tokens[id] = token
	}
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	app.Use(gin.Recovery())
	svc := service.NewModelProviderService()
	routes := router.NewRouter(handler.NewAuthHandler(), nil, nil, nil, nil, handler.NewSystemHandler(service.NewSystemService()), nil, nil, nil, nil, nil, nil, nil, nil, handler.NewProviderHandler(nil, svc), nil)
	routes.Setup(app)
	api := httptest.NewServer(app)
	t.Cleanup(func() { after9dCloseListener(t, api) })
	t.Log("owned-listener", api.URL)
	call := func(method, path, identity string, body any) (int, map[string]any, string) {
		t.Helper()
		raw, _ := json.Marshal(body)
		req, err := http.NewRequestWithContext(t.Context(), method, api.URL+path, bytes.NewReader(raw))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Content-Type", "application/json")
		if identity != "" {
			req.Header.Set("Authorization", "Bearer "+tokens[identity])
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		data, err := io.ReadAll(resp.Body)
		if err != nil {
			t.Fatal(err)
		}
		var result map[string]any
		if !strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream") {
			if err := json.Unmarshal(data, &result); err != nil {
				t.Fatal("bad API response", string(data))
			}
		}
		return resp.StatusCode, result, string(data)
	}
	requireSuccess := func(method, path string, body any) {
		t.Helper()
		status, result, raw := call(method, path, "d-owner", body)
		if status != 200 || result["code"] != float64(0) {
			t.Fatalf("%s %s: %d %s", method, path, status, raw)
		}
	}
	var calls atomic.Int32
	type providerCase struct {
		name, chat, embedding string
		dimension             int
	}
	providers := []providerCase{
		{"Avian", "deepseek/deepseek-v4-pro", "", 0},
		{"Astraflow", "claude-opus-4-7", "", 0},
		{"Perplexity", "sonar", "pplx-embed-v1-0.6b", 128},
		{"Replicate", "", "replicate/all-mpnet-base-v2:b6b7585c9640cd7a9572c6e129c9549d79c9c31f0d3fdce7baac7c67ca38f305", 2},
		{"Mistral", "magistral-medium-latest", "", 0},
		{"Upstage", "solar-pro3", "", 0},
		{"Xinference", "local-chat", "", 0},
		{"Novita", "deepseek/deepseek-v4-pro", "", 0},
		{"Moonshot", "kimi-k2.6", "", 0},
	}
	for _, p := range providers {
		if code, err := svc.AddModelProvider(p.name, "d-owner"); err != nil || code != 0 {
			t.Fatal("catalog registration", p.name, code, err)
		}
		for _, instance := range []string{"one", "two"} {
			key := p.name + "-" + instance
			// The first Xinference instance exercises its real optional-key path.
			if p.name == "Xinference" && instance == "one" {
				key = ""
			}
			fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				wantAuth := ""
				if key != "" {
					wantAuth = "Bearer " + key
				}
				if r.Header.Get("Authorization") != wantAuth {
					t.Error("credentials crossed instances")
				}
				if r.Method == "GET" {
					if r.ContentLength != 0 {
						t.Error("GET has body")
					}
					if strings.HasSuffix(r.URL.Path, "/balance/detail") {
						fmt.Fprint(w, `{"availableBalance":"12345"}`)
						return
					}
					if strings.HasSuffix(r.URL.Path, "/users/me/balance") {
						fmt.Fprint(w, `{"data":{"available_balance":0}}`)
						return
					}
					if p.name == "Avian" {
						json.NewEncoder(w).Encode([]any{map[string]any{"id": p.chat}})
					} else if p.name == "Replicate" {
						fmt.Fprint(w, `{"results":[{"owner":"owner","name":"model"}]}`)
					} else {
						json.NewEncoder(w).Encode(map[string]any{"data": []any{map[string]any{"id": p.chat}}})
					}
					return
				}
				if strings.HasSuffix(r.URL.Path, "/audio/transcriptions") {
					if err := r.ParseMultipartForm(1024); err != nil {
						t.Error(err)
						return
					}
					defer r.MultipartForm.RemoveAll()
					file, _, err := r.FormFile("file")
					if err != nil {
						t.Error(err)
						return
					}
					defer file.Close()
					raw, _ := io.ReadAll(file)
					if string(raw) != "owned audio bytes" || r.FormValue("model") != "local-asr" {
						t.Error("ASR binding lost")
					}
					fmt.Fprint(w, `{"text":"transcription"}`)
					return
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					w.WriteHeader(400)
					return
				}
				if strings.HasSuffix(r.URL.Path, "/audio/speech") {
					if body["model"] != "local-tts" || body["response_format"] != "wav" || body["stream"] != false {
						t.Error("TTS wiring lost", body)
					}
					if body["input"] == "bad" {
						w.WriteHeader(503)
						fmt.Fprint(w, "private-provider-body")
						return
					}
					w.Header().Set("Content-Type", "audio/wav")
					w.Write([]byte{0, 255, 1, 128, 9})
					return
				}
				if p.name == "Replicate" {
					if r.URL.Path != "/instance/v1/predictions" || body["version"] != p.embedding {
						t.Error("prediction wiring lost", r.URL.Path, body)
					}
					input := body["input"].(map[string]any)
					texts := []string{}
					if raw, ok := input["text_batch"].(string); ok {
						json.Unmarshal([]byte(raw), &texts)
					} else {
						texts = append(texts, input["text"].(string))
					}
					for _, text := range texts {
						if text == "bad" {
							fmt.Fprint(w, `{"id":"pred-1","status":"failed"}`)
							return
						}
					}
					output := []any{}
					for i := range texts {
						output = append(output, map[string]any{"embedding": []float64{float64(i), 0}})
					}
					json.NewEncoder(w).Encode(map[string]any{"id": "pred-1", "status": "succeeded", "output": output})
					return
				}
				if strings.HasSuffix(r.URL.Path, "/embeddings") {
					if body["model"] != p.embedding || body["dimensions"] != float64(128) || body["encoding_format"] != "base64_int8" {
						t.Error("native embedding config lost", body)
					}
					texts := body["input"].([]any)
					for _, text := range texts {
						if text == "bad" {
							w.WriteHeader(503)
							return
						}
					}
					data := []any{}
					for i := len(texts) - 1; i >= 0; i-- {
						raw := make([]byte, 128)
						raw[0] = byte(i)
						raw[1] = 255
						data = append(data, map[string]any{"index": i, "embedding": base64.StdEncoding.EncodeToString(raw)})
					}
					json.NewEncoder(w).Encode(map[string]any{"data": data})
					return
				}
				definition := dao.GetModelProviderManager().FindProvider(p.name)
				if r.URL.Path != "/instance/"+definition.URLSuffix.Chat || body["model"] != p.chat {
					t.Error("catalog chat path/name lost", r.URL.Path, body)
				}
				messages := body["messages"].([]any)
				content := messages[len(messages)-1].(map[string]any)["content"]
				if strings.Contains(fmt.Sprint(content), "bad") {
					if body["stream"] == true {
						fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n")
					} else {
						w.WriteHeader(503)
						fmt.Fprint(w, "private-provider-body")
					}
					return
				}
				answer := "answer-" + p.name + "-" + instance
				if p.name == "Mistral" {
					parts := []any{map[string]any{"type": "thinking", "thinking": []any{map[string]any{"type": "text", "text": "reason"}}}, map[string]any{"type": "text", "text": answer}}
					if body["stream"] == true {
						raw, _ := json.Marshal(map[string]any{"choices": []any{map[string]any{"delta": map[string]any{"content": parts}}}})
						fmt.Fprintf(w, "data: %s\n\ndata: [DONE]\n\n", raw)
					} else {
						json.NewEncoder(w).Encode(map[string]any{"choices": []any{map[string]any{"message": map[string]any{"content": parts}}}})
					}
					return
				}
				if body["stream"] == true {
					fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"reasoning\":\"reason\",\"reasoning_content\":\"reason\",\"content\":%q}}]}\n\ndata: [DONE]\n\n", answer)
				} else {
					json.NewEncoder(w).Encode(map[string]any{"choices": []any{map[string]any{"message": map[string]any{"content": answer, "reasoning_content": "reason", "reasoning": "reason"}}}})
				}
			}))
			t.Log("owned-provider-listener", p.name, instance, fixture.URL)
			t.Cleanup(func() { after9dCloseListener(t, fixture) })
			path := "/api/v1/providers/" + url.PathEscape(p.name) + "/instances"
			requireSuccess("POST", path, map[string]any{"instance_name": instance, "api_key": key, "region": "office", "base_url": fixture.URL + "/instance/"})
			if p.name == "Xinference" {
				for name, kind := range map[string]string{"local-chat": "chat", "local-asr": "speech2text", "local-tts": "tts"} {
					requireSuccess("POST", path+"/"+instance+"/models", map[string]any{"model_name": name, "model_type": kind, "max_tokens": 8192})
				}
			}
			var storedKey, extra string
			if err := readback.QueryRow(`SELECT i.api_key,i.extra FROM tenant_model_instance i JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.provider_name=$1 AND i.instance_name=$2`, p.name, instance).Scan(&storedKey, &extra); err != nil || storedKey != key || !strings.Contains(extra, fixture.URL) {
				t.Fatal("independent SQL readback failed", err)
			}
		}
	}
	snapshot := func() string {
		t.Helper()
		result := ""
		for _, table := range []string{"tenant_model_provider", "tenant_model_instance", "tenant_model", "t_ai_user_tenants", "t_ai_users"} {
			var hash string
			if err := readback.QueryRow("SELECT md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY id)::text,'')) FROM " + table + " t").Scan(&hash); err != nil {
				t.Fatal(err)
			}
			result += hash
		}
		return result
	}
	before := snapshot()
	binaryData, err := os.ReadFile(binary)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("candidate-cli-sha256 %x", sha256.Sum256(binaryData))
	parsed, _ := url.Parse(api.URL)
	runCLI := func(t *testing.T, identity, syntax string, success bool, want string) []byte {
		t.Helper()
		process := exec.CommandContext(t.Context(), binary, "-h", parsed.Host, "-t", tokens[identity], "-o", "json", syntax)
		output, err := process.CombinedOutput()
		t.Log("owned-cli-process", process.Process.Pid, "exit", process.ProcessState.ExitCode(), syntax)
		if (err == nil) != success || want != "" && !bytes.Contains(output, []byte(want)) || bytes.Contains(output, []byte("private-provider-body")) {
			t.Fatalf("CLI success=%v err=%v: %s", success, err, output)
		}
		if snapshot() != before {
			t.Fatal("inference/denial changed SQL")
		}
		return output
	}
	for _, p := range providers {
		for _, instance := range []string{"one", "two"} {
			t.Run(p.name+"/"+instance, func(t *testing.T) {
				if p.chat != "" {
					composite := p.chat + "@" + instance + "@" + p.name
					runCLI(t, "d-owner", fmt.Sprintf(`CHAT WITH %q MESSAGE 'q';`, composite), true, "answer-"+p.name+"-"+instance)
					runCLI(t, "d-owner", fmt.Sprintf(`STREAM CHAT WITH %q MESSAGE 'q';`, composite), true, "answer-"+p.name+"-"+instance)
					runCLI(t, "d-owner", fmt.Sprintf(`CHAT WITH %q MESSAGE 'bad';`, composite), false, "")
					runCLI(t, "d-owner", fmt.Sprintf(`STREAM CHAT WITH %q MESSAGE 'bad';`, composite), false, "")
					n := calls.Load()
					runCLI(t, "d-other", fmt.Sprintf(`CHAT WITH %q MESSAGE 'q';`, composite), false, "")
					if calls.Load() != n {
						t.Fatal("cross-tenant call reached provider")
					}
				}
				if p.embedding != "" {
					composite := p.embedding + "@" + instance + "@" + p.name
					output := runCLI(t, "d-owner", fmt.Sprintf(`EMBED TEXT %s WITH %q DIMENSION %d;`, strings.Repeat("'a' ", 33), composite, p.dimension), true, "")
					var rows []struct{ Index, Dimension int }
					if err := json.Unmarshal(output, &rows); err != nil || len(rows) != 33 || rows[32].Index != 32 || rows[32].Dimension != p.dimension {
						t.Fatal("CLI indexed dimensions lost", err)
					}
					runCLI(t, "d-owner", fmt.Sprintf(`EMBED TEXT 'bad' WITH %q DIMENSION %d;`, composite, p.dimension), false, "")
					status, response, _ := call("POST", "/api/v1/embeddings", "d-owner", map[string]any{"provider_name": p.name, "instance_name": instance, "model_name": p.embedding, "texts": []string{"a", "b"}, "dimension": p.dimension})
					if status != 200 || response["code"] != float64(0) {
						t.Fatal(response)
					}
					data := response["data"].([]any)
					for i, item := range data {
						row := item.(map[string]any)
						vector := row["embedding"].([]any)
						second := float64(0)
						if p.name == "Perplexity" {
							second = -1
						}
						if row["index"] != float64(i) || len(vector) != p.dimension || vector[0] != float64(i) || vector[1] != second {
							t.Fatal("HTTP raw vector identity lost", row)
						}
					}
				}
				runCLI(t, "d-owner", fmt.Sprintf(`LIST SUPPORTED MODELS FROM %q %q;`, p.name, instance), true, "")
				runCLI(t, "d-owner", fmt.Sprintf(`CHECK INSTANCE %q FROM %q;`, instance, p.name), true, "")
				if p.name == "Novita" || p.name == "Moonshot" {
					output := runCLI(t, "d-owner", fmt.Sprintf(`SHOW BALANCE FROM %q %q;`, p.name, instance), true, "")
					want := "1.2345"
					if p.name == "Moonshot" {
						want = "CNY"
					}
					if !bytes.Contains(output, []byte(want)) {
						t.Fatal("balance native unit lost", string(output))
					}
				}
				if p.name == "Xinference" {
					file := filepath.Join(t.TempDir(), "audio.wav")
					if err := os.WriteFile(file, []byte("owned audio bytes"), 0600); err != nil {
						t.Fatal(err)
					}
					runCLI(t, "d-owner", fmt.Sprintf(`ASR WITH 'local-asr@%s@Xinference' AUDIO %q;`, instance, file), true, "transcription")
					output := runCLI(t, "d-owner", fmt.Sprintf(`TTS WITH 'local-tts@%s@Xinference' TEXT 'hello' FORMAT wav;`, instance), true, "")
					var rows []map[string]any
					if err := json.Unmarshal(output, &rows); err != nil || len(rows) != 1 {
						t.Fatal(err)
					}
					raw, err := base64.StdEncoding.Strict().DecodeString(rows[0]["audio"].(string))
					if err != nil || !bytes.Equal(raw, []byte{0, 255, 1, 128, 9}) || rows[0]["format"] != "wav" {
						t.Fatal("audio bytes corrupted", err)
					}
					runCLI(t, "d-owner", fmt.Sprintf(`TTS WITH 'local-tts@%s@Xinference' TEXT 'bad' FORMAT wav;`, instance), false, "")
					n := calls.Load()
					runCLI(t, "d-owner", fmt.Sprintf(`STREAM TTS WITH 'local-tts@%s@Xinference' TEXT 'hello' FORMAT wav;`, instance), false, "")
					if calls.Load() != n {
						t.Fatal("unsupported stream reached provider")
					}
					if err := os.Remove(file); err != nil {
						t.Fatal(err)
					}
				}
				n := calls.Load()
				status, denied, _ := call("POST", "/api/v1/embeddings", "", map[string]any{"provider_name": p.name, "instance_name": instance, "model_name": p.embedding, "texts": []string{"a"}})
				if status != 401 || denied["code"] == float64(0) || calls.Load() != n || snapshot() != before {
					t.Fatal("denial or inference side effects")
				}
			})
		}
	}
	t.Log("catalog -> real router/JWT/service -> independent PostgreSQL -> controlled native provider HTTP; SQL unchanged", calls.Load())
}
