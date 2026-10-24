package handler_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

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

// Provider HTTP is controlled; the CLI binary, registered JWT routes, factories,
// tenant/instance binding, PostgreSQL commits and independent reads are real.
func TestAfterD7ProvidersHTTPCLIAndSQL(t *testing.T) {
	config, cliBinary := os.Getenv("MULTIRAG_AFTERD7_A_CONFIG"), os.Getenv("MULTIRAG_AFTERD7_A_CLI")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned after-d7 A PostgreSQL config required")
		}
		t.Skip("requires owned after-d7 A PostgreSQL config and candidate CLI")
	}
	if cliBinary == "" {
		t.Fatal("candidate CLI required")
	}
	projectRoot := filepath.Join("..", "..")
	if _, err := os.Stat("go.mod"); err == nil {
		projectRoot = "."
	}
	t.Chdir(projectRoot)
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal("private config load failed")
	}
	if db := server.GetConfig().Database; db.Database != "multirag_afterd7_a_20261024" || db.Host != "127.0.0.1" {
		t.Fatal("unowned database")
	}
	if err := dao.InitDB(); err != nil {
		t.Fatal("scratch database initialization failed", err)
	}
	pool, err := dao.DB.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	dsn, err := dao.PostgresDSN(server.GetConfig().Database)
	if err != nil {
		t.Fatal(err)
	}
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { readback.Close() })
	tables := []string{"tenant_model", "tenant_model_instance", "tenant_model_provider", "t_ai_user_tenants", "t_ai_users"}
	t.Cleanup(func() {
		for _, table := range tables {
			if _, err := readback.Exec("DELETE FROM " + table); err != nil {
				t.Error("owned SQL cleanup failed", table, err)
			}
			var count int
			if err := readback.QueryRow("SELECT count(*) FROM " + table).Scan(&count); err != nil || count != 0 {
				t.Error("owned SQL cleanup readback", table, count, err)
			}
		}
		t.Log("owned PostgreSQL cleanup independently read back zero rows in five tables")
	})
	if err := server.InitVariables(nil); err != nil {
		t.Fatal(err)
	}
	local.InitAdminStatus(0, "owned after-d7 provider acceptance")
	active := "1"
	tokens := map[string]string{}
	for _, id := range []string{"afterd7-a-owner", "afterd7-a-other"} {
		email := id + "@owned.invalid"
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
	routes := router.NewRouter(handler.NewAuthHandler(), nil, nil, nil, nil, handler.NewSystemHandler(service.NewSystemService()), nil, nil, nil, nil, nil, nil, nil, nil, nil, handler.NewProviderHandler(nil, svc), nil)
	routes.Setup(app)
	api := httptest.NewServer(app)
	t.Cleanup(func() { afterD7CloseListener(t, api) })
	parsedAPI, _ := url.Parse(api.URL)
	client := &http.Client{Timeout: 15 * time.Second}
	request := func(method, path, identity string, body any) (int, map[string]any, []byte) {
		t.Helper()
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		req, err := http.NewRequestWithContext(t.Context(), method, api.URL+path, bytes.NewReader(raw))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Content-Type", "application/json")
		if identity != "" {
			req.Header.Set("Authorization", "Bearer "+tokens[identity])
		}
		resp, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		data, err := io.ReadAll(io.LimitReader(resp.Body, 25*1024*1024))
		if err != nil {
			t.Fatal(err)
		}
		var envelope map[string]any
		if !strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream") {
			if err := json.Unmarshal(data, &envelope); err != nil {
				t.Fatal("invalid application response", string(data))
			}
		}
		if bytes.Contains(data, []byte("private-provider-body")) {
			t.Fatal("provider body leaked through API")
		}
		return resp.StatusCode, envelope, data
	}
	success := func(method, path string, body any) map[string]any {
		t.Helper()
		status, envelope, raw := request(method, path, "afterd7-a-owner", body)
		if status != http.StatusOK || envelope["code"] != float64(0) {
			t.Fatalf("%s %s failed: %d %s", method, path, status, raw)
		}
		return envelope
	}
	snapshot := func() string {
		t.Helper()
		var result string
		for _, table := range tables {
			var value string
			if err := readback.QueryRow("SELECT md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY id)::text,'')) FROM " + table + " t").Scan(&value); err != nil {
				t.Fatal("independent SQL snapshot", err)
			}
			result += value
		}
		return result
	}
	before := ""
	runCLI := func(identity, syntax string, valid bool, want string) []byte {
		t.Helper()
		ctx, cancel := context.WithTimeout(t.Context(), 20*time.Second)
		defer cancel()
		command := exec.CommandContext(ctx, cliBinary, "-h", parsedAPI.Host, "-t", tokens[identity], "-o", "json", syntax)
		output, err := command.CombinedOutput()
		if command.ProcessState != nil {
			t.Log("owned-cli-process", command.Process.Pid, "exit", command.ProcessState.ExitCode(), syntax)
		}
		if valid != (err == nil) || want != "" && !bytes.Contains(output, []byte(want)) || bytes.Contains(output, []byte("private-provider-body")) || !valid && bytes.Contains(output, []byte("[DONE]")) {
			t.Fatalf("CLI expected success %v: %s: %v %s", valid, syntax, err, output)
		}
		if before != "" && snapshot() != before {
			t.Fatal("inference or denial mutated persisted rows")
		}
		return output
	}
	cliBytes, err := os.ReadFile(cliBinary)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("candidate CLI SHA256 %x", sha256.Sum256(cliBytes))
	var documentFetches, providerCalls atomic.Int64
	documents := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { documentFetches.Add(1); fmt.Fprint(w, "%PDF-1.7 owned") }))
	t.Cleanup(func() { afterD7CloseListener(t, documents) })
	audio := make([]byte, 46)
	copy(audio, "RIFF")
	binary.LittleEndian.PutUint32(audio[4:], 38)
	copy(audio[8:], "WAVEfmt ")
	binary.LittleEndian.PutUint32(audio[16:], 16)
	binary.LittleEndian.PutUint16(audio[20:], 1)
	binary.LittleEndian.PutUint16(audio[22:], 1)
	binary.LittleEndian.PutUint32(audio[24:], 16000)
	binary.LittleEndian.PutUint32(audio[28:], 32000)
	binary.LittleEndian.PutUint16(audio[32:], 2)
	binary.LittleEndian.PutUint16(audio[34:], 16)
	copy(audio[36:], "data")
	binary.LittleEndian.PutUint32(audio[40:], 2)
	audio[44], audio[45] = 1, 128
	var mediaDir string
	t.Cleanup(func() {
		if _, err := os.Stat(mediaDir); !os.IsNotExist(err) {
			t.Error("owned media directory remains", mediaDir, err)
		}
		t.Log("owned-media-directory-absent-readback", mediaDir)
	})
	mediaDir = t.TempDir()
	t.Log("owned-media-directory", mediaDir)
	audioFile, pdfFile := filepath.Join(mediaDir, "owned.wav"), filepath.Join(mediaDir, "owned.pdf")
	if err := os.WriteFile(audioFile, audio, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(pdfFile, []byte("%PDF-1.7 owned"), 0600); err != nil {
		t.Fatal(err)
	}
	type providerCase struct{ name, chat, embed, rerank, asr, ocr, parse, tts string }
	providers := []providerCase{
		{name: "MinerU", parse: "vlm"},
		{name: "Replicate", chat: "meta/meta-llama-3-70b-instruct"},
		{name: "Xinference", chat: "served-chat"},
		{name: "TogetherAI", chat: "openai/gpt-oss-20b"},
		{name: "302.AI", chat: "gpt-4o", embed: "jina-embeddings-v3", rerank: "jina-reranker-v2-base-multilingual", asr: "whisper-v3-turbo", ocr: "mistral-ocr-latest", parse: "vlm"},
		{name: "JieKouAI", chat: "zai-org/glm-4.5", embed: "text-embedding-3-large", rerank: "baai/bge-reranker-v2-m3"},
		{name: "CometAPI", chat: "gpt-4o", embed: "text-embedding-3-small", asr: "whisper-1", tts: "tts-1"},
	}
	states := map[string]*atomic.Value{}
	versionModel := "replicate/hello-world:" + strings.Repeat("a", 64)
	for _, provider := range providers {
		if code, err := svc.AddModelProvider(provider.name, "afterd7-a-owner"); err != nil || code != common.CodeSuccess {
			t.Fatal("catalog provider registration", provider.name, code, err)
		}
		definition := dao.GetModelProviderManager().FindProvider(provider.name)
		if definition == nil {
			t.Fatal("provider missing from real catalog")
		}
		for _, instance := range []string{"one", "two"} {
			key := "controlled-" + provider.name + "-" + instance
			if provider.name == "Xinference" && instance == "one" {
				key = ""
			}
			state := &atomic.Value{}
			state.Store("running")
			states[provider.name+instance] = state
			fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				providerCalls.Add(1)
				path := strings.TrimPrefix(r.URL.Path, "/instance/")
				wantAuth := ""
				if key != "" && !(provider.name == "CometAPI" && r.Method == http.MethodGet) {
					wantAuth = "Bearer " + key
				}
				if r.Header.Get("Authorization") != wantAuth {
					t.Error("provider credentials crossed SQL instances", provider.name, instance)
				}
				answer := "answer-" + provider.name + "-" + instance
				if strings.HasPrefix(path, "replicate-stream/") {
					w.Header().Set("Content-Type", "text/event-stream")
					fmt.Fprintf(w, "event: output\ndata: %s\n\n", answer)
					if strings.HasSuffix(path, "cancel") {
						fmt.Fprint(w, "event: done\ndata: {\"reason\":\"canceled\"}\n\n")
					} else {
						fmt.Fprint(w, "event: done\ndata: {}\n\n")
					}
					return
				}
				if r.Method == http.MethodGet {
					if r.ContentLength != 0 {
						t.Error("provider GET had a body")
					}
					switch {
					case provider.name == "Replicate" && strings.HasPrefix(path, "v1/predictions/"):
						fmt.Fprintf(w, `{"id":"pred-%s","status":"succeeded","output":%q}`, instance, answer)
					case provider.parse != "" && strings.HasPrefix(path, definition.URLSuffix.DocumentParse+"/"):
						fmt.Fprintf(w, `{"code":0,"data":{"task_id":"job-%s","state":%q,"full_zip_url":%q}}`, instance, state.Load().(string), documents.URL+"/result.zip")
					case provider.name == "Replicate":
						fmt.Fprint(w, `{"results":[{"owner":"meta","name":"meta-llama-3-70b-instruct"}]}`)
					case provider.name == "TogetherAI":
						json.NewEncoder(w).Encode([]map[string]any{{"id": provider.chat}})
					default:
						if path != definition.URLSuffix.Models {
							t.Error("configured discovery suffix lost", path)
						}
						json.NewEncoder(w).Encode(map[string]any{"data": []map[string]any{{"id": provider.chat}}})
					}
					return
				}
				if path == definition.URLSuffix.ASR && provider.asr != "" {
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
					input, _ := io.ReadAll(file)
					if !bytes.Equal(input, audio) || r.FormValue("model") != provider.asr {
						t.Error("ASR binary/model binding lost")
					}
					fmt.Fprint(w, `{"text":"recognized"}`)
					return
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error("invalid provider JSON")
					return
				}
				switch {
				case provider.parse != "" && path == definition.URLSuffix.DocumentParse:
					if body["model_version"] != provider.parse {
						t.Error("parse model identity lost")
					}
					if strings.Contains(fmt.Sprint(body["url"]), "bad") {
						fmt.Fprint(w, `{"code":1,"msg":"private-provider-body"}`)
					} else {
						fmt.Fprintf(w, `{"code":0,"data":{"task_id":"job-%s"}}`, instance)
					}
				case provider.ocr != "" && path == definition.URLSuffix.OCR:
					if body["model"] != provider.ocr {
						t.Error("OCR binding lost")
					}
					fmt.Fprint(w, `{"pages":[{"index":1,"markdown":"second"},{"index":0,"markdown":"first"}]}`)
				case provider.tts != "" && path == definition.URLSuffix.TTS:
					if body["model"] != provider.tts || body["response_format"] != "wav" || body["voice"] != "alloy" {
						t.Error("speech SQL/options lost")
					}
					if body["input"] == "bad" {
						fmt.Fprint(w, `{"error":"private-provider-body"}`)
					} else {
						w.Header().Set("Content-Type", "audio/wav")
						w.Write(audio)
					}
				case provider.embed != "" && path == definition.URLSuffix.Embedding:
					if body["model"] != provider.embed || body["dimensions"] != float64(2) {
						t.Error("embedding SQL/config lost", body)
					}
					inputs := body["input"].([]any)
					if len(inputs) > 0 && inputs[0] == "bad" {
						fmt.Fprint(w, `{"data":[{"index":0,"embedding":[null,1]}]}`)
					} else {
						var vectors []map[string]any
						for index := len(inputs) - 1; index >= 0; index-- {
							vectors = append(vectors, map[string]any{"index": index, "embedding": []float64{float64(index), 0}})
						}
						json.NewEncoder(w).Encode(map[string]any{"data": vectors})
					}
				case provider.rerank != "" && path == definition.URLSuffix.Rerank:
					if body["model"] != provider.rerank || body["top_n"] != float64(2) {
						t.Error("rerank SQL/config lost")
					}
					if body["query"] == "bad" {
						fmt.Fprint(w, `{"results":[{"index":0,"relevance_score":null}]}`)
					} else {
						fmt.Fprint(w, `{"results":[{"index":1,"relevance_score":0},{"index":0,"relevance_score":0.9}]}`)
					}
				default:
					prompt := ""
					if provider.name == "Replicate" {
						if version, ok := body["version"].(string); ok {
							if path != definition.URLSuffix.Chat || version != versionModel {
								t.Error("version prediction route/identity lost", path)
							}
						} else if path != definition.URLSuffix.Models+"/"+provider.chat+"/predictions" {
							t.Error("official prediction route lost", path)
						}
						prompt = fmt.Sprint(body["input"].(map[string]any)["prompt"])
						status := "succeeded"
						if strings.Contains(prompt, "bad") {
							status = "failed"
						}
						if prompt == "cancel" {
							status = "canceled"
						}
						if prompt == "poll" {
							fmt.Fprintf(w, `{"id":"pred-%s","status":"processing","urls":{"get":"http://%s/instance/v1/predictions/pred-%s"}}`, instance, r.Host, instance)
						} else if strings.Contains(prompt, "sse") {
							mode := "ok"
							if prompt == "cancel-sse" {
								mode = "cancel"
							}
							fmt.Fprintf(w, `{"id":"pred-%s","status":"starting","urls":{"stream":"http://%s/instance/replicate-stream/%s"}}`, instance, r.Host, mode)
						} else {
							fmt.Fprintf(w, `{"id":"pred-%s","status":%q,"output":[%q]}`, instance, status, answer)
						}
						return
					}
					if path != definition.URLSuffix.Chat || body["model"] != provider.chat {
						t.Error("chat SQL endpoint/model binding lost", path, body)
					}
					messages := body["messages"].([]any)
					prompt = fmt.Sprint(messages[len(messages)-1].(map[string]any)["content"])
					if strings.Contains(prompt, "bad") {
						if body["stream"] == true {
							w.Header().Set("Content-Type", "text/event-stream")
							fmt.Fprint(w, "data: invalid\n\n")
						} else {
							w.WriteHeader(503)
							fmt.Fprint(w, "private-provider-body")
						}
					} else if body["stream"] == true {
						w.Header().Set("Content-Type", "text/event-stream")
						fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":%q},\"finish_reason\":\"stop\"}]}\n\n", answer)
					} else {
						json.NewEncoder(w).Encode(map[string]any{"choices": []map[string]any{{"message": map[string]any{"content": answer, "reasoning_content": "reason"}}}})
					}
				}
			}))
			t.Cleanup(func() { afterD7CloseListener(t, fixture) })
			t.Log("owned-provider-listener", provider.name, instance, fixture.URL)
			base := fixture.URL + "/instance"
			if provider.name == "Xinference" && instance == "one" {
				base += "/v1"
				runCLI("afterd7-a-owner", fmt.Sprintf(`CREATE PROVIDER %q INSTANCE %q KEY '' URL %q REGION 'office';`, provider.name, instance, base), true, "")
			} else {
				success(http.MethodPost, "/api/v1/providers/"+url.PathEscape(provider.name)+"/instances", map[string]any{"instance_name": instance, "api_key": key, "base_url": base, "region": "office"})
			}
			if provider.name == "Xinference" {
				runCLI("afterd7-a-owner", fmt.Sprintf(`ADD MODEL %q TO PROVIDER %q INSTANCE %q WITH TOKENS 2048 CHAT;`, provider.chat, provider.name, instance), true, "")
				var modelType, modelExtra string
				if err := readback.QueryRow(`SELECT m.model_type,m.extra FROM tenant_model m JOIN tenant_model_instance i ON i.id=m.instance_id JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.provider_name=$1 AND i.instance_name=$2 AND m.model_name=$3`, provider.name, instance, provider.chat).Scan(&modelType, &modelExtra); err != nil || modelType != "chat" || !strings.Contains(modelExtra, `"max_tokens":2048`) {
					t.Fatal("custom keyless model SQL readback failed", err)
				}
			}
			if provider.name == "Replicate" {
				runCLI("afterd7-a-owner", fmt.Sprintf(`ADD MODEL %q TO PROVIDER %q INSTANCE %q WITH TOKENS 2048 CHAT;`, versionModel, provider.name, instance), true, "")
			}
			var storedKey, extra string
			if err := readback.QueryRow(`SELECT i.api_key,i.extra FROM tenant_model_instance i JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.provider_name=$1 AND i.instance_name=$2 AND p.tenant_id='afterd7-a-owner-tenant'`, provider.name, instance).Scan(&storedKey, &extra); err != nil || storedKey != key || !strings.Contains(extra, base) {
				t.Fatal("independent committed instance readback failed", provider.name, instance, err)
			}
		}
	}
	before = snapshot()
	for _, provider := range providers {
		for _, instance := range []string{"one", "two"} {
			t.Run(provider.name+"/"+instance, func(t *testing.T) {
				composite := func(model string) string { return model + "@" + instance + "@" + provider.name }
				if provider.chat != "" {
					model := composite(provider.chat)
					runCLI("afterd7-a-owner", fmt.Sprintf(`CHAT WITH %q MESSAGE 'q';`, model), true, "answer-"+provider.name+"-"+instance)
					runCLI("afterd7-a-owner", fmt.Sprintf(`STREAM CHAT WITH %q MESSAGE 'q';`, model), true, "answer-"+provider.name+"-"+instance)
					for _, prefix := range []string{"", "STREAM "} {
						runCLI("afterd7-a-owner", fmt.Sprintf(`%sCHAT WITH %q MESSAGE 'bad';`, prefix, model), false, "")
					}
					if provider.name == "Replicate" {
						runCLI("afterd7-a-owner", fmt.Sprintf(`CHAT WITH %q MESSAGE 'q';`, composite(versionModel)), true, "answer-Replicate-")
						runCLI("afterd7-a-owner", fmt.Sprintf(`CHAT WITH %q MESSAGE 'poll';`, model), true, "answer-Replicate-")
						runCLI("afterd7-a-owner", fmt.Sprintf(`STREAM CHAT WITH %q MESSAGE 'sse';`, model), true, "answer-Replicate-")
						runCLI("afterd7-a-owner", fmt.Sprintf(`STREAM CHAT WITH %q MESSAGE 'cancel';`, model), false, "")
						_, _, raw := request(http.MethodPost, "/api/v1/chat/completions", "afterd7-a-owner", map[string]any{"provider_name": provider.name, "instance_name": instance, "model_name": provider.chat, "message": "cancel-sse", "stream": true})
						if bytes.Contains(raw, []byte("[DONE]")) || !bytes.Contains(raw, []byte("error")) {
							t.Fatal("cancelled Replicate SSE reported success", string(raw))
						}
					}
					runCLI("afterd7-a-owner", fmt.Sprintf(`LIST SUPPORTED MODELS FROM %q %q;`, provider.name, instance), true, provider.chat)
					if provider.name != "CometAPI" {
						runCLI("afterd7-a-owner", fmt.Sprintf(`CHECK INSTANCE %q FROM %q;`, instance, provider.name), true, "")
					}
					n := providerCalls.Load()
					runCLI("afterd7-a-other", fmt.Sprintf(`CHAT WITH %q MESSAGE 'q';`, model), false, "")
					status, envelope, _ := request(http.MethodPost, "/api/v1/chat/completions", "", map[string]any{"provider_name": provider.name, "instance_name": instance, "model_name": provider.chat, "message": "q"})
					if status != http.StatusUnauthorized || envelope["code"] == float64(0) || providerCalls.Load() != n {
						t.Fatal("chat denial reached provider")
					}
				}
				if provider.embed != "" {
					output := runCLI("afterd7-a-owner", fmt.Sprintf(`EMBED TEXT %s WITH %q DIMENSION 2;`, strings.Repeat("'text' ", 33), composite(provider.embed)), true, "")
					var rows []struct{ Index, Dimension int }
					if err := json.Unmarshal(output, &rows); err != nil || len(rows) != 33 || rows[32].Index != 32 || rows[32].Dimension != 2 {
						t.Fatal("CLI embedding batch/global order lost", err)
					}
					runCLI("afterd7-a-owner", fmt.Sprintf(`EMBED TEXT 'bad' WITH %q DIMENSION 2;`, composite(provider.embed)), false, "")
					result := success(http.MethodPost, "/api/v1/embeddings", map[string]any{"provider_name": provider.name, "instance_name": instance, "model_name": provider.embed, "texts": []string{"a", "b"}, "dimension": 2})
					if data := result["data"].([]any); len(data) != 2 || data[0].(map[string]any)["index"] != float64(0) {
						t.Fatal("HTTP embedding ordering lost")
					}
				}
				if provider.rerank != "" {
					result := success(http.MethodPost, "/api/v1/rerank", map[string]any{"provider_name": provider.name, "instance_name": instance, "model_name": provider.rerank, "query": "q", "documents": []string{"a", "b"}})
					data := result["data"].([]any)
					if len(data) != 2 || data[1].(map[string]any)["relevance_score"] != float64(0) {
						t.Fatal("legal zero rerank score lost")
					}
					_, result, _ = request(http.MethodPost, "/api/v1/rerank", "afterd7-a-owner", map[string]any{"provider_name": provider.name, "instance_name": instance, "model_name": provider.rerank, "query": "bad", "documents": []string{"a", "b"}})
					if result["code"] == float64(0) {
						t.Fatal("malformed rerank accepted")
					}
				}
				if provider.asr != "" {
					runCLI("afterd7-a-owner", fmt.Sprintf(`ASR WITH %q AUDIO %q;`, composite(provider.asr), audioFile), true, "recognized")
				}
				if provider.ocr != "" {
					runCLI("afterd7-a-owner", fmt.Sprintf(`OCR WITH %q FILE %q;`, composite(provider.ocr), pdfFile), true, "first")
					runCLI("afterd7-a-owner", fmt.Sprintf(`OCR WITH %q URL %q;`, composite(provider.ocr), documents.URL+"/doc.pdf"), true, "second")
				}
				if provider.parse != "" {
					for _, kind := range []string{"FILE", "URL"} {
						output := runCLI("afterd7-a-owner", fmt.Sprintf(`PARSE WITH %q %s %q;`, composite(provider.parse), kind, documents.URL+"/doc.pdf"), true, "pending")
						if bytes.Contains(output, []byte("content")) {
							t.Fatal("pending task exposed content")
						}
					}
					n := providerCalls.Load()
					runCLI("afterd7-a-owner", fmt.Sprintf(`PARSE WITH %q FILE %q;`, composite(provider.parse), pdfFile), false, "")
					if providerCalls.Load() != n {
						t.Fatal("unsupported local parse upload reached provider")
					}
					runCLI("afterd7-a-owner", fmt.Sprintf(`PARSE WITH %q URL %q;`, composite(provider.parse), documents.URL+"/bad.pdf"), false, "")
					runCLI("afterd7-a-owner", fmt.Sprintf(`SHOW '%s@%s' TASK 'job-%s';`, instance, provider.name, instance), true, "running")
					states[provider.name+instance].Store("done")
					runCLI("afterd7-a-owner", fmt.Sprintf(`SHOW '%s@%s' TASK 'job-%s';`, instance, provider.name, instance), true, "result.zip")
					n = providerCalls.Load()
					runCLI("afterd7-a-other", fmt.Sprintf(`SHOW '%s@%s' TASK 'job-%s';`, instance, provider.name, instance), false, "")
					runCLI("afterd7-a-owner", fmt.Sprintf(`LIST '%s@%s' TASKS;`, instance, provider.name), false, "")
					if providerCalls.Load() != n {
						t.Fatal("task denial/unsupported list reached provider")
					}
					if provider.name == "MinerU" {
						runCLI("afterd7-a-owner", fmt.Sprintf(`CHECK INSTANCE %q FROM %q;`, instance, provider.name), false, "")
					}
				}
				if provider.tts != "" {
					output := runCLI("afterd7-a-owner", fmt.Sprintf(`TTS WITH %q TEXT 'speak' PARAM '{"voice":"alloy"}' FORMAT wav SAVE %q;`, composite(provider.tts), mediaDir), true, "")
					var rows []map[string]any
					if err := json.Unmarshal(output, &rows); err != nil || len(rows) != 1 {
						t.Fatal("CLI TTS result invalid", err)
					}
					decoded, err := base64.StdEncoding.Strict().DecodeString(rows[0]["audio"].(string))
					if err != nil || !bytes.Equal(decoded, audio) || rows[0]["format"] != "wav" || string(decoded[8:12]) != "WAVE" || binary.LittleEndian.Uint32(decoded[24:28]) != 16000 {
						t.Fatal("audio bytes/format/header corrupted", err)
					}
					savedPath, ok := rows[0]["saved_path"].(string)
					if !ok || filepath.Dir(savedPath) != mediaDir || filepath.Ext(savedPath) != ".wav" {
						t.Fatal("saved audio path outside owned directory", savedPath)
					}
					saved, err := os.ReadFile(savedPath)
					if err != nil || !bytes.Equal(saved, audio) {
						t.Fatal("saved audio bytes corrupted", err)
					}
					if err := os.Remove(savedPath); err != nil {
						t.Fatal(err)
					}
					if _, err := os.Stat(savedPath); !os.IsNotExist(err) {
						t.Fatal("owned speech file remains", err)
					}
					runCLI("afterd7-a-owner", fmt.Sprintf(`TTS WITH %q TEXT 'bad' PARAM '{"voice":"alloy"}' FORMAT wav;`, composite(provider.tts)), false, "")
				}
				if snapshot() != before {
					t.Fatal("HTTP inference/failure/denial mutated SQL")
				}
			})
		}
	}
	// Prove disabled instances and incorrect catalog capabilities stop before HTTP.
	if _, err := readback.Exec(`UPDATE tenant_model_instance SET status='disabled' WHERE id IN (SELECT i.id FROM tenant_model_instance i JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.provider_name='Xinference' AND i.instance_name='one')`); err != nil {
		t.Fatal(err)
	}
	before = snapshot()
	n := providerCalls.Load()
	runCLI("afterd7-a-owner", `CHAT WITH 'served-chat@one@Xinference' MESSAGE 'q';`, false, "")
	_, denied, _ := request(http.MethodPost, "/api/v1/audio/transcriptions", "afterd7-a-owner", map[string]any{"provider_name": "302.AI", "instance_name": "one", "model_name": "gpt-4o", "file": base64.StdEncoding.EncodeToString(audio)})
	if denied["code"] == float64(0) || providerCalls.Load() != n || snapshot() != before {
		t.Fatal("disabled/capability denial side effects")
	}
	_, denied, _ = request(http.MethodPost, "/api/v1/providers/Replicate/instances", "afterd7-a-owner", map[string]any{"instance_name": "missing-key", "api_key": "", "base_url": documents.URL})
	if denied["code"] == float64(0) || providerCalls.Load() != n || snapshot() != before {
		t.Fatal("missing key create wrote rows or called provider")
	}
	_, denied, _ = request(http.MethodPost, "/api/v1/providers/Xinference/instances", "afterd7-a-other", map[string]any{"instance_name": "foreign", "api_key": "", "base_url": documents.URL})
	if denied["code"] == float64(0) || providerCalls.Load() != n || snapshot() != before {
		t.Fatal("foreign create wrote rows or called provider")
	}
	if documentFetches.Load() != 0 {
		t.Fatal("URL forwarding fetched documents locally", documentFetches.Load())
	}
	t.Log("actual CLI/JWT/SQL/provider HTTP accepted; independent inference and denial row hashes stable; controlled requests", providerCalls.Load(), "document fetches", documentFetches.Load())
}

func afterD7CloseListener(t *testing.T, fixture *httptest.Server) {
	t.Helper()
	address := fixture.Listener.Addr().String()
	fixture.Close()
	connection, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
	if err == nil {
		connection.Close()
		t.Error("owned listener still reachable", address)
	}
	t.Log("owned-listener-absent-readback", address)
}
