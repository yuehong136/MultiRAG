package service_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
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
	"sync"
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

// SQL, JWT routes and the candidate CLI are real. Vendor protocols are controlled.
func TestProviderFixesHTTPCLIAndSQL(t *testing.T) {
	config, binary := os.Getenv("MULTIRAG_AFTER0FB_D_CONFIG"), os.Getenv("MULTIRAG_AFTER0FB_D_CLI")
	resources := os.Getenv("MULTIRAG_PROVIDER_TEST_JOURNAL")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned provider-fixes D configuration required")
		}
		t.Skip("requires owned provider-fixes D PostgreSQL configuration and candidate CLI")
	}
	if binary == "" || resources == "" {
		t.Fatal("candidate CLI and owned resource registry required")
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
		t.Fatal("private configuration load failed")
	}
	if cfg := server.GetConfig().Database; !strings.HasPrefix(cfg.Database, "multirag_after0fb_d_") || cfg.Host != "127.0.0.1" || cfg.Schema != "usr_ai" {
		t.Fatal("unowned database")
	}
	if err := dao.InitDB(); err != nil {
		t.Fatal("owned schema initialization failed", err)
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
		t.Fatal("independent database connection failed")
	}
	t.Cleanup(func() { readback.Close() })
	tables := []string{"tenant_model", "tenant_model_instance", "tenant_model_provider", "t_ai_user_tenants", "t_ai_users"}
	for _, table := range tables {
		var count int
		if err := readback.QueryRow("SELECT count(*) FROM " + table).Scan(&count); err != nil || count != 0 {
			t.Fatal("scratch table not empty", table, count, err)
		}
	}
	t.Cleanup(func() {
		for _, table := range tables {
			if _, err := readback.Exec("DELETE FROM " + table); err != nil {
				t.Error("owned SQL cleanup", table, err)
			}
			var count int
			if err := readback.QueryRow("SELECT count(*) FROM " + table).Scan(&count); err != nil || count != 0 {
				t.Error("independent SQL cleanup readback", table, count, err)
			}
		}
		t.Log("independent SQL readback: five owned tables empty")
	})
	if err := server.InitVariables(nil); err != nil {
		t.Fatal(err)
	}
	local.InitAdminStatus(0, "owned provider-fixes D acceptance")
	var registryMu sync.Mutex
	record := func(value map[string]any) {
		t.Helper()
		if value["case"] == nil {
			value["case"] = t.Name()
		}
		value["database"] = server.GetConfig().Database.Database
		if value["run_id"] == nil {
			value["run_id"] = os.Getenv("MULTIRAG_AFTER0FB_D_RUN") + "/" + t.Name()
		}
		registryMu.Lock()
		defer registryMu.Unlock()
		file, err := os.OpenFile(resources, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
		if err != nil {
			t.Fatal(err)
		}
		defer file.Close()
		if err := json.NewEncoder(file).Encode(value); err != nil {
			t.Fatal(err)
		}
	}
	bind := func(label string, h http.Handler) *httptest.Server {
		t.Helper()
		runID := fmt.Sprintf("%s/%s/%d", os.Getenv("MULTIRAG_AFTER0FB_D_RUN"), label, time.Now().UnixNano())
		record(map[string]any{"action": "intent", "run_id": runID, "kind": "http-listener", "case": label, "label": label, "listen": "127.0.0.1:0"})
		srv := httptest.NewUnstartedServer(h)
		address := srv.Listener.Addr().String()
		record(map[string]any{"action": "bound", "run_id": runID, "case": label, "label": label, "address": address})
		srv.Start()
		t.Cleanup(func() {
			srv.CloseClientConnections()
			srv.Close()
			connection, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
			if err == nil {
				connection.Close()
				t.Error("owned listener remains", label, address)
			}
			record(map[string]any{"action": "closed-readback", "run_id": runID, "case": label, "label": label, "address": address, "absent": err != nil})
		})
		return srv
	}
	tokens := map[string]string{}
	active := "1"
	for _, id := range []string{"after0fb-d-owner", "after0fb-d-other"} {
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
	routes := router.NewRouter(handler.NewAuthHandler(), nil, nil, nil, nil, handler.NewSystemHandler(service.NewSystemService()), nil, nil, nil, nil, nil, nil, nil, nil, handler.NewProviderHandler(nil, svc), nil)
	routes.Setup(app)
	api := bind("application", app)
	apiURL, _ := url.Parse(api.URL)
	client := &http.Client{Timeout: 15 * time.Second}
	t.Cleanup(client.CloseIdleConnections)
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
		raw, err = io.ReadAll(io.LimitReader(resp.Body, 25*1024*1024))
		if err != nil {
			t.Fatal(err)
		}
		if bytes.Contains(raw, []byte("private-provider-body")) {
			t.Fatal("provider body leaked")
		}
		var result map[string]any
		if !strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream") {
			if err := json.Unmarshal(raw, &result); err != nil {
				t.Fatal("invalid application envelope", string(raw))
			}
		}
		return resp.StatusCode, result, raw
	}
	success := func(method, path string, body any) map[string]any {
		t.Helper()
		status, result, raw := request(method, path, "after0fb-d-owner", body)
		if status != 200 || result["code"] != float64(0) {
			t.Fatalf("%s %s: %d %s", method, path, status, raw)
		}
		return result
	}
	snapshot := func() string {
		t.Helper()
		result := ""
		for _, table := range tables {
			var digest string
			if err := readback.QueryRow("SELECT md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY id)::text,'')) FROM " + table + " t").Scan(&digest); err != nil {
				t.Fatal(err)
			}
			result += digest
		}
		return result
	}
	private := filepath.Dir(config)
	cliConfig, pdf := filepath.Join(private, "cli.json"), filepath.Join(private, "document.pdf")
	for path, raw := range map[string][]byte{cliConfig: []byte(`{}`), pdf: []byte("%PDF-1.7 owned-local-document")} {
		record(map[string]any{"action": "intent", "kind": "private-file", "path": path})
		if err := os.WriteFile(path, raw, 0o600); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			if err := os.Remove(path); err != nil {
				t.Error(err)
			}
			_, err := os.Stat(path)
			record(map[string]any{"action": "file-removed-readback", "path": path, "absent": os.IsNotExist(err)})
		})
	}
	activeCase := t.Name()
	before := ""
	runCLI := func(identity, syntax string, valid bool, want string) []byte {
		t.Helper()
		ctx, cancel := context.WithTimeout(t.Context(), 20*time.Second)
		defer cancel()
		command := exec.CommandContext(ctx, binary, "-f", cliConfig, "-h", apiURL.Host, "-t", tokens[identity], "-o", "json", syntax)
		cliRunID := fmt.Sprintf("%s/cli/%d", os.Getenv("MULTIRAG_AFTER0FB_D_RUN"), time.Now().UnixNano())
		record(map[string]any{"action": "intent", "run_id": cliRunID, "kind": "cli-process", "case": activeCase, "syntax": strings.Split(syntax, " ")[0]})
		output, err := command.CombinedOutput()
		if command.ProcessState != nil {
			record(map[string]any{"action": "cli-exit-readback", "run_id": cliRunID, "kind": "cli-process", "case": activeCase, "pid": command.Process.Pid, "exit": command.ProcessState.ExitCode(), "absent": command.ProcessState.Exited()})
		}
		if valid != (err == nil) || want != "" && !bytes.Contains(output, []byte(want)) || bytes.Contains(output, []byte("private-provider-body")) || !valid && bytes.Contains(output, []byte("[DONE]")) {
			t.Fatalf("CLI expected success %v: %s: %v %s", valid, syntax, err, output)
		}
		if before != "" && snapshot() != before {
			t.Fatal("inference/denial mutated SQL configuration")
		}
		return output
	}
	binaryBytes, err := os.ReadFile(binary)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("current source CLI SHA256 %x", sha256.Sum256(binaryBytes))
	type providerCase struct{ name, chat, embed, rerank, ocr, parse string }
	providers := []providerCase{
		{name: "TokenHub", chat: "gpt-4o-mini", embed: "text-embedding-3-small"},
		{name: "LongCat", chat: "LongCat-Flash-Chat"},
		{name: "xAI", chat: "grok-4"},
		{name: "VolcEngine", chat: "doubao-seed-2-0-pro-260215", embed: "doubao-embedding-vision-250615"},
		{name: "Gitee", chat: "qwen3-8b", embed: "BAAI/bge-m3", rerank: "BAAI/bge-reranker-v2-m3"},
		{name: "Replicate", rerank: "yxzwayne/bge-reranker-v2-m3:7f7c6e9d18336e2cbf07d88e9362d881d2fe4d6a9854ec1260f115cabc106a8c"},
		{name: "MinerU", parse: "vlm"}, {name: "mineru_local", parse: "pipeline"},
		{name: "PaddleOCR", ocr: "PP-OCRv5"}, {name: "PaddleOCR_Local", ocr: "PaddleOCR-VL"},
	}
	var calls atomic.Int64
	for _, p := range providers {
		before = ""
		record(map[string]any{"action": "intent", "run_id": os.Getenv("MULTIRAG_AFTER0FB_D_RUN") + "/provider/" + p.name, "kind": "sql-provider", "tenant": "after0fb-d-owner-tenant", "provider": p.name})
		success(http.MethodPut, "/api/v1/providers/", map[string]any{"provider_name": p.name})
		definition := dao.GetModelProviderManager().FindProvider(p.name)
		if definition == nil {
			t.Fatal("catalog missing", p.name)
		}
		for _, instance := range []string{"one", "two"} {
			activeCase = p.name + "/" + instance
			caseID := os.Getenv("MULTIRAG_AFTER0FB_D_RUN") + "/" + activeCase
			before = ""
			key := "controlled-" + p.name + "-" + instance
			if p.name == "mineru_local" || p.name == "PaddleOCR_Local" {
				key = ""
			}
			fixture := bind(p.name+"/"+instance, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				path := strings.TrimPrefix(r.URL.Path, "/instance/")
				expected := "Bearer " + key
				if key == "" {
					expected = ""
				}
				if path == "result" {
					if r.Header.Get("Authorization") != "" {
						t.Error("Paddle result credential leak")
					}
					fmt.Fprintln(w, `{"result":{"layoutParsingResults":[{"markdown":{"text":"recognized"}}]}}`)
					return
				}
				if r.Header.Get("Authorization") != expected {
					t.Error("SQL instance key/region lost", p.name, instance, path)
				}
				if r.Method == "GET" {
					switch {
					case p.name == "MinerU":
						fmt.Fprint(w, `{"code":0,"data":{"task_id":"job-1","state":"done","full_zip_url":"https://files.invalid/result.zip"}}`)
					case p.name == "mineru_local":
						fmt.Fprint(w, `{"task_id":"job-1","status":"success","results":{"doc":{"md_content":"parsed"}}}`)
					case p.name == "PaddleOCR":
						fmt.Fprintf(w, `{"data":{"state":"done","resultUrl":{"jsonUrl":"http://%s/instance/result"}}}`, r.Host)
					case p.name == "Replicate":
						fmt.Fprint(w, `{"id":"pred-1","status":"succeeded","output":[0,2]}`)
					default:
						if path != definition.URLSuffix.Models {
							t.Error("actual list suffix", p.name, path)
						}
						fmt.Fprint(w, `{"data":[{"id":"discovered"}]}`)
					}
					return
				}
				if p.name == "xAI" && path == "stt" {
					if err := r.ParseMultipartForm(1 << 20); err != nil {
						t.Error(err)
						return
					}
					defer r.MultipartForm.RemoveAll()
					f, _, err := r.FormFile("file")
					if err != nil {
						t.Error(err)
						return
					}
					defer f.Close()
					raw, _ := io.ReadAll(f)
					if string(raw) != "wav-data" || r.FormValue("model") != "stt" {
						t.Error("ASR body")
					}
					fmt.Fprint(w, `{"text":"recognized"}`)
					return
				}
				if p.name == "mineru_local" {
					if r.ParseMultipartForm(1<<20) != nil {
						t.Error("local parse multipart")
					}
					defer r.MultipartForm.RemoveAll()
					w.WriteHeader(202)
					fmt.Fprint(w, `{"task_id":"job-1"}`)
					return
				}
				if p.name == "PaddleOCR" {
					if r.ParseMultipartForm(1<<20) != nil {
						t.Error("OCR multipart")
					}
					defer r.MultipartForm.RemoveAll()
					fmt.Fprint(w, `{"data":{"jobId":"job-1"}}`)
					return
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				if p.name == "xAI" && path == "tts" {
					if body["text"] != "hello" || body["voice_id"] != "eve" {
						t.Error("xAI speech body", body)
					}
					w.Header().Set("Content-Type", "audio/mpeg")
					fmt.Fprint(w, "mp3-data")
					return
				}
				if p.name == "MinerU" {
					if body["url"] != "https://files.invalid/document.pdf" || body["model_version"] != "vlm" {
						t.Error("MinerU body")
					}
					fmt.Fprint(w, `{"code":0,"data":{"task_id":"job-1"}}`)
					return
				}
				if p.name == "PaddleOCR_Local" {
					fmt.Fprint(w, `{"errorCode":0,"result":{"layoutParsingResults":[{"markdown":{"text":"recognized"}}]}}`)
					return
				}
				if p.name == "Replicate" {
					if body["version"] != "7f7c6e9d18336e2cbf07d88e9362d881d2fe4d6a9854ec1260f115cabc106a8c" || body["input"].(map[string]any)["input_list"] != `[["q","a"],["q","b"]]` {
						t.Error("Replicate rerank prediction body", body)
					}
					fmt.Fprintf(w, `{"id":"pred-1","status":"processing","urls":{"get":"http://%s/instance/pred-1"}}`, r.Host)
					return
				}
				switch path {
				case definition.URLSuffix.Embedding:
					if p.name == "VolcEngine" {
						fmt.Fprint(w, `{"data":{"embedding":[0,1]}}`)
					} else {
						fmt.Fprint(w, `{"data":[{"index":1,"embedding":[1,0]},{"index":0,"embedding":[0,1]}]}`)
					}
				case definition.URLSuffix.Rerank:
					fmt.Fprint(w, `{"results":[{"index":1,"relevance_score":2},{"index":0,"relevance_score":0}]}`)
				case definition.URLSuffix.Chat:
					answer := "answer-" + p.name + "-" + instance
					if body["stream"] == true {
						w.Header().Set("Content-Type", "text/event-stream")
						fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":%q}}]}\n\ndata: [DONE]\n\n", answer)
					} else {
						fmt.Fprintf(w, `{"choices":[{"message":{"content":%q}}]}`, answer)
					}
				default:
					t.Error("unexpected controlled path", p.name, path)
					w.WriteHeader(404)
				}
			}))
			base := fixture.URL + "/instance"
			record(map[string]any{"action": "intent", "kind": "sql-instance", "case": activeCase, "run_id": caseID, "tenant": "after0fb-d-owner-tenant", "provider": p.name, "instance": instance})
			runCLI("after0fb-d-owner", fmt.Sprintf(`CREATE PROVIDER %q INSTANCE %q KEY %q URL %q REGION 'office';`, p.name, instance, key, base), true, "")
			if p.name == "TokenHub" {
				runCLI("after0fb-d-owner", fmt.Sprintf(`ADD MODEL %q TO PROVIDER %q INSTANCE %q WITH EMBEDDING TOKENS 2048;`, p.embed, p.name, instance), true, "")
			}
			if p.name == "mineru_local" {
				runCLI("after0fb-d-owner", fmt.Sprintf(`ADD MODEL 'pipeline' TO PROVIDER %q INSTANCE %q WITH DOC_PARSE TOKENS 2048;`, p.name, instance), true, "")
			}
			if p.name == "xAI" {
				runCLI("after0fb-d-owner", fmt.Sprintf(`ADD MODEL 'stt' TO PROVIDER 'xAI' INSTANCE %q WITH ASR TOKENS 2048;`, instance), true, "")
			}
			var storedKey, extra, id string
			if err := readback.QueryRow(`SELECT i.id,i.api_key,i.extra FROM tenant_model_instance i JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.provider_name=$1 AND i.instance_name=$2 AND p.tenant_id='after0fb-d-owner-tenant'`, p.name, instance).Scan(&id, &storedKey, &extra); err != nil || storedKey != key || !strings.Contains(extra, base) || !strings.Contains(extra, "office") {
				t.Fatal("independent SQL instance readback", p.name, instance, err)
			}
			record(map[string]any{"action": "committed-readback", "kind": "sql-instance", "case": activeCase, "run_id": caseID, "id": id, "provider": p.name, "instance": instance})
			before = snapshot()
			t.Run(p.name+"/"+instance, func(t *testing.T) {
				composite := func(name string) string { return name + "@" + instance + "@" + p.name }
				identity := func(name string) map[string]any {
					return map[string]any{"provider_name": p.name, "instance_name": instance, "model_name": name}
				}
				if p.chat != "" {
					runCLI("after0fb-d-owner", fmt.Sprintf(`CHAT WITH %q MESSAGE 'q';`, composite(p.chat)), true, "answer-"+p.name+"-"+instance)
					runCLI("after0fb-d-owner", fmt.Sprintf(`STREAM CHAT WITH %q MESSAGE 'q';`, composite(p.chat)), true, "answer-"+p.name+"-"+instance)
				}
				if p.embed != "" {
					runCLI("after0fb-d-owner", fmt.Sprintf(`EMBED TEXT 'a' 'b' WITH %q DIMENSION 2;`, composite(p.embed)), true, "dimension")
					bound, err := svc.GetEmbeddingModel("after0fb-d-owner-tenant", composite(p.embed))
					if err != nil {
						t.Fatal(err)
					}
					vectors, err := bound.Embed([]string{"a", "b"})
					if err != nil || len(vectors) != 2 || vectors[0].Embedding[0] != 0 {
						t.Fatal(vectors, err)
					}
				}
				if p.rerank != "" {
					body := identity(p.rerank)
					body["query"], body["documents"], body["top_n"] = "q", []string{"a", "b"}, 0
					data := success(http.MethodPost, "/api/v1/rerank", body)["data"].([]any)
					if len(data) != 2 || data[1].(map[string]any)["relevance_score"] != float64(0) {
						t.Fatal("zero/index lost", data)
					}
					bound, err := svc.GetRerankModel("after0fb-d-owner-tenant", composite(p.rerank))
					if err != nil {
						t.Fatal(err)
					}
					scores, err := bound.Rerank("q", []string{"a", "b"})
					if err != nil || len(scores) != 2 || scores[0] != 0 || scores[1] != 2 {
						t.Fatal("bound index reconstruction", scores, err)
					}
				}
				if p.parse != "" {
					syntax := fmt.Sprintf(`PARSE WITH %q FILE 'https://files.invalid/document.pdf';`, composite(p.parse))
					if p.name == "mineru_local" {
						syntax = fmt.Sprintf(`PARSE WITH %q FILE %q;`, composite(p.parse), pdf)
					}
					runCLI("after0fb-d-owner", syntax, true, "job-1")
					result := success(http.MethodGet, "/api/v1/providers/"+p.name+"/instances/"+instance+"/tasks/job-1", nil)
					if result["data"].(map[string]any)["status"] != "success" {
						t.Fatal(result)
					}
				}
				if p.ocr != "" {
					body := identity(p.ocr)
					body["content"] = base64.StdEncoding.EncodeToString([]byte("%PDF-1.7 owned-local-document"))
					result := success(http.MethodPost, "/api/v1/file/ocr", body)
					if result["data"].(map[string]any)["text"] != "recognized" {
						t.Fatal(result)
					}
				}
				if p.name == "xAI" {
					body := identity("stt")
					body["file"] = base64.StdEncoding.EncodeToString([]byte("wav-data"))
					body["format"] = "wav"
					result := success(http.MethodPost, "/api/v1/audio/transcriptions", body)
					if result["data"].(map[string]any)["text"] != "recognized" {
						t.Fatal(result)
					}
					runCLI("after0fb-d-owner", fmt.Sprintf(`TTS WITH %q TEXT 'hello';`, composite("eve")), true, "")
					body = identity("eve")
					body["text"], body["stream"] = "hello", true
					status, _, raw := request(http.MethodPost, "/api/v1/audio/speech", "after0fb-d-owner", body)
					if status != 200 || !bytes.Contains(raw, []byte("[DONE]")) || !bytes.Contains(raw, []byte("bXAzLWRhdGE=")) {
						t.Fatal("binary to SSE facade", status, string(raw))
					}
				}
				if p.name == "LongCat" || p.name == "xAI" || p.name == "TokenHub" || p.name == "VolcEngine" {
					runCLI("after0fb-d-owner", fmt.Sprintf(`LIST SUPPORTED MODELS FROM %q %q;`, p.name, instance), true, "discovered")
					discovered, err := svc.ListSupportedModels(p.name, instance, "after0fb-d-owner")
					if err != nil || len(discovered) != 1 || discovered[0] != "discovered" {
						t.Fatal("SQL-bound real discovery", discovered, err)
					}
				}
				n := calls.Load()
				path := "/api/v1/providers/" + p.name + "/instances/" + instance + "/models"
				_, result, _ := request(http.MethodGet, path, "after0fb-d-other", nil)
				if result["code"] == float64(0) || calls.Load() != n || snapshot() != before {
					t.Fatal("tenant denial had side effects")
				}
				if snapshot() != before {
					t.Fatal("inference changed SQL")
				}
			})
		}
	}
	t.Log("10 existing providers / 20 SQL instances; actual JWT HTTP and source CLI, bound indexed embed/rerank, xAI media, hosted/local OCR and parse, region/credential isolation and independent denial readback")
}
