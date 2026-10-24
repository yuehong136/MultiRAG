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
func TestAfter2836ModelsHTTPCLIAndSQL(t *testing.T) {
	config, binary := os.Getenv("MULTIRAG_AFTER2836_F_CONFIG"), os.Getenv("MULTIRAG_AFTER2836_F_CLI")
	resources := os.Getenv("MULTIRAG_AFTER2836_F_RESOURCES")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned after2836 F configuration required")
		}
		t.Skip("requires owned after2836 F PostgreSQL configuration and candidate CLI")
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
	if cfg := server.GetConfig().Database; cfg.Database != "multirag_after2836_f_20261024" || cfg.Host != "127.0.0.1" || cfg.Schema != "usr_ai" {
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
	local.InitAdminStatus(0, "owned after2836 F acceptance")
	var registryMu sync.Mutex
	record := func(value map[string]any) {
		t.Helper()
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
		record(map[string]any{"action": "intent", "kind": "http-listener", "label": label, "listen": "127.0.0.1:0"})
		srv := httptest.NewUnstartedServer(h)
		address := srv.Listener.Addr().String()
		record(map[string]any{"action": "bound", "label": label, "address": address})
		srv.Start()
		t.Cleanup(func() {
			srv.CloseClientConnections()
			srv.Close()
			connection, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
			if err == nil {
				connection.Close()
				t.Error("owned listener remains", label, address)
			}
			record(map[string]any{"action": "closed-readback", "label": label, "address": address, "absent": err != nil})
		})
		return srv
	}
	tokens := map[string]string{}
	active := "1"
	for _, id := range []string{"after2836-f-owner", "after2836-f-other"} {
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
		status, result, raw := request(method, path, "after2836-f-owner", body)
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
	before := ""
	runCLI := func(identity, syntax string, valid bool, want string) []byte {
		t.Helper()
		ctx, cancel := context.WithTimeout(t.Context(), 20*time.Second)
		defer cancel()
		command := exec.CommandContext(ctx, binary, "-f", cliConfig, "-h", apiURL.Host, "-t", tokens[identity], "-o", "json", syntax)
		output, err := command.CombinedOutput()
		if command.ProcessState != nil {
			t.Log("owned-cli-process", command.Process.Pid, "exit", command.ProcessState.ExitCode())
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
	type providerCase struct {
		name, chat, embed, rerank, parse string
		dynamic                          bool
	}
	providers := []providerCase{
		{name: "mineru_local", parse: "pipeline", dynamic: true},
		{name: "TogetherAI", embed: "intfloat/multilingual-e5-large-instruct"},
		{name: "n1n", chat: "gpt-4o-mini", embed: "text-embedding-3-small", rerank: "BAAI/bge-reranker-v2-m3"},
		{name: "Xinference", chat: "tenant-chat", embed: "tenant-embed", rerank: "tenant-rerank", dynamic: true},
		{name: "Novita", embed: "baai/bge-m3", rerank: "baai/bge-reranker-v2-m3"},
		{name: "GPUStack", chat: "tenant-chat", dynamic: true},
		{name: "Azure-OpenAI", chat: "tenant-chat", embed: "tenant-embed", dynamic: true},
	}
	var calls atomic.Int64
	for _, p := range providers {
		definition := dao.GetModelProviderManager().FindProvider(p.name)
		if definition == nil {
			t.Fatal("catalog missing", p.name)
		}
		success(http.MethodPut, "/api/v1/providers/", map[string]any{"provider_name": p.name})
		if p.name != "mineru_local" && p.name != "Xinference" {
			previous := snapshot()
			n := calls.Load()
			_, result, _ := request(http.MethodPost, "/api/v1/providers/"+url.PathEscape(p.name)+"/instances", "after2836-f-owner", map[string]any{"instance_name": "missing-key", "base_url": "http://127.0.0.1:1", "api_key": ""})
			if result["code"] == float64(0) || snapshot() != previous || calls.Load() != n {
				t.Fatal("required-key denial wrote SQL or contacted provider", p.name)
			}
		}
		for _, instance := range []string{"one", "two"} {
			key := "controlled-" + p.name + "-" + instance
			if instance == "one" && (p.name == "mineru_local" || p.name == "Xinference") {
				key = ""
			}
			var taskReads atomic.Int32
			fixture := bind(p.name+"/"+instance, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				wantAuth := ""
				if key != "" {
					wantAuth = "Bearer " + key
				}
				if p.name == "Azure-OpenAI" {
					if r.Header.Get("api-key") != key || r.Header.Get("Authorization") != "" || r.URL.Query().Get("api-version") != "2024-10-21" {
						t.Error("Azure tenant auth/version lost")
					}
				} else if r.Header.Get("Authorization") != wantAuth {
					t.Error("tenant credential isolation lost", p.name, instance)
				}
				path := strings.TrimPrefix(r.URL.Path, "/instance/")
				if p.name == "Azure-OpenAI" {
					path = strings.TrimPrefix(r.URL.Path, "/instance/openai/")
				}
				if r.Method == http.MethodGet {
					if p.parse != "" {
						if path != "tasks/job-"+instance+"/result" {
							t.Error("incorrect task path", path)
						}
						if taskReads.Add(1) == 1 {
							w.WriteHeader(202)
							fmt.Fprint(w, `{"status":"running"}`)
						} else {
							fmt.Fprint(w, `{"results":{"b.pdf":{"md_content":"second"},"a.pdf":{"md_content":"first"}}}`)
						}
						return
					}
					if path != definition.URLSuffix.Models {
						t.Error("discovery path lost", p.name, path)
					}
					name := p.chat
					if name == "" {
						name = p.embed
					}
					if p.name == "Azure-OpenAI" {
						name = "resource-base-model"
					}
					if p.name == "TogetherAI" {
						json.NewEncoder(w).Encode([]map[string]any{{"id": name}})
					} else {
						json.NewEncoder(w).Encode(map[string]any{"data": []map[string]any{{"id": name, "object": "model"}}})
					}
					return
				}
				if p.parse != "" {
					if path != "file_parse" || r.ParseMultipartForm(1024) != nil {
						t.Error("local parser multipart path lost")
					}
					if r.MultipartForm != nil {
						defer r.MultipartForm.RemoveAll()
					}
					file, _, err := r.FormFile("files")
					if err != nil {
						t.Error(err)
						return
					}
					defer file.Close()
					raw, _ := io.ReadAll(file)
					if string(raw) != "%PDF-1.7 owned-local-document" || r.FormValue("backend") != p.parse {
						t.Error("local parse bytes/backend lost")
					}
					w.WriteHeader(202)
					fmt.Fprintf(w, `{"task_id":"job-%s"}`, instance)
					return
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				if p.name == "Azure-OpenAI" {
					if body["model"] != nil {
						t.Error("Azure model must be in deployment path")
					}
					if strings.HasPrefix(path, "deployments/"+p.chat+"/") {
						path = strings.TrimPrefix(path, "deployments/"+p.chat+"/")
					} else if strings.HasPrefix(path, "deployments/"+p.embed+"/") {
						path = strings.TrimPrefix(path, "deployments/"+p.embed+"/")
					} else {
						t.Error("Azure used base model rather than user deployment", path)
					}
				}
				switch path {
				case definition.URLSuffix.Embedding:
					if body["dimensions"] != float64(2) || p.name != "Azure-OpenAI" && body["model"] != p.embed {
						t.Error("embedding SQL dimensions/model lost", p.name)
					}
					inputs := body["input"].([]any)
					if inputs[0] == "bad" {
						fmt.Fprint(w, `{"data":[{"index":0,"embedding":[null,1]}]}`)
						return
					}
					data := []map[string]any{}
					for i := len(inputs) - 1; i >= 0; i-- {
						data = append(data, map[string]any{"index": i, "embedding": []float64{float64(i), 0}})
					}
					json.NewEncoder(w).Encode(map[string]any{"data": data})
				case definition.URLSuffix.Rerank:
					if body["model"] != p.rerank || body["top_n"] != float64(2) {
						t.Error("rerank model/top_n lost")
					}
					if body["query"] == "bad" {
						fmt.Fprint(w, `{"results":[{"index":0,"relevance_score":null}]}`)
					} else {
						fmt.Fprint(w, `{"results":[{"index":1,"relevance_score":0},{"index":0,"relevance_score":0.9}]}`)
					}
				case definition.URLSuffix.Chat:
					if p.name != "Azure-OpenAI" && body["model"] != p.chat {
						t.Error("chat model lost")
					}
					if p.name == "n1n" && body["thinking"].(map[string]any)["type"] != "disabled" {
						t.Error("n1n thinking false lost")
					}
					message := fmt.Sprint(body["messages"])
					if strings.Contains(message, "filtered") {
						if body["stream"] == true {
							w.Header().Set("Content-Type", "text/event-stream")
							fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\ndata: {\"choices\":[{\"delta\":{},\"finish_reason\":\"content_filter\"}]}\n")
						} else {
							fmt.Fprint(w, `{"choices":[{"message":{"content":"partial"},"finish_reason":"content_filter"}]}`)
						}
						return
					}
					if strings.Contains(message, "bad") {
						w.WriteHeader(502)
						fmt.Fprint(w, "private-provider-body")
						return
					}
					answer := "answer-" + p.name + "-" + instance
					if body["stream"] == true {
						w.Header().Set("Content-Type", "text/event-stream")
						fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":%q},\"finish_reason\":\"stop\"}]}\n", answer)
					} else {
						json.NewEncoder(w).Encode(map[string]any{"choices": []map[string]any{{"message": map[string]any{"content": answer}}}})
					}
				default:
					t.Error("unexpected provider path", p.name, path)
					w.WriteHeader(404)
				}
			}))
			base := fixture.URL + "/instance"
			if p.name == "Azure-OpenAI" {
				base += "/openai"
			}
			if instance == "one" && (p.name == "GPUStack" || p.name == "Xinference") {
				base += "/v1"
			}
			runCLI("after2836-f-owner", fmt.Sprintf(`CREATE PROVIDER %q INSTANCE %q KEY %q URL %q REGION 'office';`, p.name, instance, key, base), true, "")
			for _, declaration := range []struct{ name, kind string }{{p.chat, "CHAT"}, {p.embed, "EMBEDDING"}, {p.rerank, "RERANKER"}, {p.parse, "DOC_PARSE"}} {
				if p.dynamic && declaration.name != "" {
					runCLI("after2836-f-owner", fmt.Sprintf(`ADD MODEL %q TO PROVIDER %q INSTANCE %q WITH %s TOKENS 2048;`, declaration.name, p.name, instance, declaration.kind), true, "")
					var typ, extra string
					wantType := strings.ToLower(declaration.kind)
					if wantType == "reranker" {
						wantType = "rerank"
					}
					if err := readback.QueryRow(`SELECT m.model_type,m.extra FROM tenant_model m JOIN tenant_model_instance i ON i.id=m.instance_id JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.provider_name=$1 AND i.instance_name=$2 AND m.model_name=$3`, p.name, instance, declaration.name).Scan(&typ, &extra); err != nil || typ != wantType || !strings.Contains(extra, `"max_tokens":2048`) {
						t.Fatal("independent model capability readback", p.name, instance, err)
					}
				}
			}
			var storedKey, extra string
			if err := readback.QueryRow(`SELECT i.api_key,i.extra FROM tenant_model_instance i JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.provider_name=$1 AND i.instance_name=$2 AND p.tenant_id='after2836-f-owner-tenant'`, p.name, instance).Scan(&storedKey, &extra); err != nil || storedKey != key || !strings.Contains(extra, base) {
				t.Fatal("independent committed instance readback", p.name, instance, err)
			}
		}
	}
	before = snapshot()
	for _, p := range providers {
		for _, instance := range []string{"one", "two"} {
			t.Run(p.name+"/"+instance, func(t *testing.T) {
				composite := func(name string) string { return name + "@" + instance + "@" + p.name }
				if p.chat != "" {
					runCLI("after2836-f-owner", fmt.Sprintf(`CHAT WITH %q MESSAGE 'q' ;`, composite(p.chat)), true, "answer-"+p.name+"-"+instance)
					runCLI("after2836-f-owner", fmt.Sprintf(`STREAM CHAT WITH %q MESSAGE 'q' ;`, composite(p.chat)), true, "answer-"+p.name+"-"+instance)
					runCLI("after2836-f-owner", fmt.Sprintf(`CHAT WITH %q MESSAGE 'bad';`, composite(p.chat)), false, "")
					if p.name == "n1n" || p.name == "GPUStack" || p.name == "Azure-OpenAI" {
						for _, prefix := range []string{"", "STREAM "} {
							runCLI("after2836-f-owner", fmt.Sprintf(`%sCHAT WITH %q MESSAGE 'filtered';`, prefix, composite(p.chat)), false, "")
						}
						for _, stream := range []bool{false, true} {
							_, result, raw := request(http.MethodPost, "/api/v1/chat/completions", "after2836-f-owner", map[string]any{"provider_name": p.name, "instance_name": instance, "model_name": p.chat, "message": "filtered", "thinking": false, "stream": stream})
							if !stream && result["code"] == float64(0) || stream && (!bytes.Contains(raw, []byte("error")) || bytes.Contains(raw, []byte("[DONE]"))) || snapshot() != before {
								t.Fatal("filtered HTTP response reported success or changed SQL", p.name, stream, string(raw))
							}
						}
					}
				}
				if p.embed != "" {
					output := runCLI("after2836-f-owner", fmt.Sprintf(`EMBED TEXT %s WITH %q DIMENSION 2;`, strings.Repeat("'text' ", 33), composite(p.embed)), true, "")
					var rows []struct{ Index, Dimension int }
					if err := json.Unmarshal(output, &rows); err != nil || len(rows) != 33 || rows[32].Index != 32 || rows[32].Dimension != 2 {
						t.Fatal("CLI global embedding order", err)
					}
					runCLI("after2836-f-owner", fmt.Sprintf(`EMBED TEXT 'bad' WITH %q DIMENSION 2;`, composite(p.embed)), false, "")
				}
				if p.rerank != "" {
					body := map[string]any{"provider_name": p.name, "instance_name": instance, "model_name": p.rerank, "query": "q", "documents": []string{"a", "b"}}
					data := success(http.MethodPost, "/api/v1/rerank", body)["data"].([]any)
					if len(data) != 2 || data[1].(map[string]any)["relevance_score"] != float64(0) {
						t.Fatal("zero rank lost")
					}
					body["query"] = "bad"
					_, result, _ := request(http.MethodPost, "/api/v1/rerank", "after2836-f-owner", body)
					if result["code"] == float64(0) {
						t.Fatal("bad rerank succeeded")
					}
				}
				if p.parse != "" {
					runCLI("after2836-f-owner", fmt.Sprintf(`PARSE WITH %q FILE %q;`, composite(p.parse), pdf), true, "pending")
					runCLI("after2836-f-owner", fmt.Sprintf(`SHOW %q TASK %q;`, instance+"@"+p.name, "job-"+instance), true, "running")
					runCLI("after2836-f-owner", fmt.Sprintf(`SHOW %q TASK %q;`, instance+"@"+p.name, "job-"+instance), true, "first")
					n := calls.Load()
					runCLI("after2836-f-owner", fmt.Sprintf(`PARSE WITH %q URL 'https://files.invalid/document.pdf';`, composite(p.parse)), false, "")
					if calls.Load() != n {
						t.Fatal("unsupported URL reached local parser")
					}
				} else {
					want := p.chat
					if want == "" {
						want = p.embed
					}
					if p.name == "Azure-OpenAI" {
						want = "resource-base-model"
					}
					runCLI("after2836-f-owner", fmt.Sprintf(`LIST SUPPORTED MODELS FROM %q %q;`, p.name, instance), true, want)
					runCLI("after2836-f-owner", fmt.Sprintf(`CHECK INSTANCE %q FROM %q;`, instance, p.name), true, "")
				}
				name, endpoint, body := p.chat, "/api/v1/chat/completions", map[string]any{"message": "q"}
				if name == "" {
					name, endpoint, body = p.embed, "/api/v1/embeddings", map[string]any{"texts": []string{"a"}, "dimension": 2}
				}
				if p.parse != "" {
					name, endpoint, body = p.parse, "/api/v1/file/parse", map[string]any{"content": base64.StdEncoding.EncodeToString([]byte("%PDF-1.7 owned-local-document"))}
				}
				body["provider_name"], body["instance_name"], body["model_name"] = p.name, instance, name
				for _, identity := range []string{"after2836-f-other", ""} {
					n := calls.Load()
					_, result, _ := request(http.MethodPost, endpoint, identity, body)
					if result["code"] == float64(0) || calls.Load() != n || snapshot() != before {
						t.Fatal("denial reached provider or wrote SQL", p.name, identity)
					}
				}
			})
		}
	}
	if snapshot() != before {
		t.Fatal("inference or discovery changed SQL configuration")
	}
	rejectWithoutEffects := func(endpoint string, body map[string]any) {
		t.Helper()
		previous, n := snapshot(), calls.Load()
		_, result, _ := request(http.MethodPost, endpoint, "after2836-f-owner", body)
		if result["code"] == float64(0) || snapshot() != previous || calls.Load() != n {
			t.Fatal("disabled/capability denial wrote SQL or called vendor")
		}
	}
	var instanceID, modelID string
	if err := readback.QueryRow(`SELECT i.id,m.id FROM tenant_model m JOIN tenant_model_instance i ON i.id=m.instance_id JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.tenant_id='after2836-f-owner-tenant' AND p.provider_name='Xinference' AND i.instance_name='one' AND m.model_name='tenant-embed'`).Scan(&instanceID, &modelID); err != nil {
		t.Fatal(err)
	}
	embedBody := map[string]any{"provider_name": "Xinference", "instance_name": "one", "model_name": "tenant-embed", "texts": []string{"q"}, "dimension": 2}
	for _, toggle := range []struct{ table, id string }{{"tenant_model_instance", instanceID}, {"tenant_model", modelID}} {
		if result, err := readback.Exec("UPDATE "+toggle.table+" SET status='disabled' WHERE id=$1", toggle.id); err != nil {
			t.Fatal(err)
		} else if count, _ := result.RowsAffected(); count != 1 {
			t.Fatal("disable did not target one owned row")
		}
		t.Cleanup(func() {
			if _, err := readback.Exec("UPDATE "+toggle.table+" SET status='active' WHERE id=$1", toggle.id); err != nil {
				t.Error("owned row restore", err)
			}
		})
		rejectWithoutEffects("/api/v1/embeddings", embedBody)
		if _, err := readback.Exec("UPDATE "+toggle.table+" SET status='active' WHERE id=$1", toggle.id); err != nil {
			t.Fatal(err)
		}
		var status string
		if err := readback.QueryRow("SELECT status FROM "+toggle.table+" WHERE id=$1", toggle.id).Scan(&status); err != nil || status != "active" || snapshot() != before {
			t.Fatal("independent restoration readback failed", status, err)
		}
	}
	rejectWithoutEffects("/api/v1/embeddings", map[string]any{"provider_name": "GPUStack", "instance_name": "one", "model_name": "tenant-chat", "texts": []string{"q"}, "dimension": 2})
	t.Log("configured seven providers/fourteen isolated instances; selected registered HTTP/current CLI cases passed with independent SQL readback; vendor calls", calls.Load())
}
