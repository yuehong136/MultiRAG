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

// This acceptance uses the actual CLI process, registered JWT routes, scratch SQL
// and provider drivers. Only the external provider boundary is controlled HTTP.
func TestAfter410GoHTTPCLIAndSQL(t *testing.T) {
	config, binary := os.Getenv("MULTIRAG_AFTER410_CONFIG"), os.Getenv("MULTIRAG_AFTER410_CLI")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned after410 scratch config required")
		}
		t.Skip("requires owned after410 scratch database")
	}
	if binary == "" {
		t.Fatal("candidate CLI binary required")
	}
	t.Log("owned-test-process", os.Getpid())
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal("load scratch config")
	}
	if server.GetConfig().Database.Database != "multirag_after410_a_20261024" {
		t.Fatal("unowned scratch database")
	}
	if err := dao.InitDB(); err != nil {
		t.Fatal(err)
	}
	pool, _ := dao.DB.DB()
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
	if err := server.InitVariables(nil); err != nil {
		t.Fatal(err)
	}
	local.InitAdminStatus(0, "owned after410 acceptance")
	tokens := map[string]string{}
	for _, id := range []string{"owner", "foreign", "revoked", "disabled"} {
		status := "1"
		if id == "disabled" {
			status = "0"
		}
		email := "a410-" + id + "@fixture.invalid"
		if err := dao.DB.Create(&entity.User{ID: "a410-" + id, Email: email, Nickname: id, Status: &status}).Error; err != nil {
			t.Fatal(err)
		}
		relation := "1"
		if id == "revoked" {
			relation = "0"
		}
		if err := dao.DB.Create(&entity.UserTenant{ID: "a410-" + id, UserID: "a410-" + id, TenantID: "a410-" + id, Role: "owner", InvitedBy: "a410-" + id, Status: &relation}).Error; err != nil {
			t.Fatal(err)
		}
		token, err := utility.SignUserJWT(email, server.GetSecretKey())
		if err != nil {
			t.Fatal(err)
		}
		tokens[id] = token
	}
	t.Cleanup(func() {
		for _, table := range []any{&entity.TenantModel{}, &entity.TenantModelInstance{}, &entity.TenantModelProvider{}, &entity.UserTenant{}, &entity.User{}} {
			if err := dao.DB.Where("id LIKE ?", "a410-%").Delete(table).Error; err != nil {
				t.Error("scratch row cleanup", err)
			}
		}
	})
	var calls atomic.Int32
	state := atomic.Value{}
	state.Store("pending")
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("Authorization") != "Bearer controlled-gitee" {
			t.Error("SQL credential lost")
		}
		switch r.URL.Path {
		case "/async/documents/parse":
			if err := r.ParseMultipartForm(1 << 20); err != nil {
				t.Error(err)
				w.WriteHeader(400)
				return
			}
			defer r.MultipartForm.RemoveAll()
			if r.FormValue("model") != "MinerU2.5" {
				t.Error("parse model lost")
			}
			if r.FormValue("file") == "" {
				f, _, err := r.FormFile("file")
				if err != nil {
					t.Error(err)
					return
				}
				defer f.Close()
				data, _ := io.ReadAll(f)
				if string(data) != "document bytes" {
					t.Error("CLI file bytes lost")
				}
			}
			fmt.Fprint(w, `{"task_id":"job-1","status":"pending"}`)
		case "/tasks":
			fmt.Fprintf(w, `{"items":[{"task_id":"job-1","status":%q}]}`, state.Load())
		case "/task/job-1/status":
			fmt.Fprintf(w, `{"task_id":"job-1","status":%q}`, state.Load())
		case "/task/job-1/get":
			fmt.Fprint(w, `{"segments":[{"index":1,"content":"page one"},{"index":2,"content":"page two"}]}`)
		default:
			w.WriteHeader(404)
		}
	}))
	defer provider.Close()
	t.Log("owned-provider-listener", provider.URL)
	if err := dao.DB.Create(&entity.TenantModelProvider{ID: "a410-gitee", TenantID: "a410-owner", ProviderName: "Gitee"}).Error; err != nil {
		t.Fatal(err)
	}
	extra, _ := json.Marshal(map[string]string{"base_url": provider.URL, "region": "default"})
	for _, name := range []string{"one", "disabled"} {
		status := "active"
		if name == "disabled" {
			status = "disabled"
		}
		if err := dao.DB.Create(&entity.TenantModelInstance{ID: "a410-gitee-" + name, ProviderID: "a410-gitee", InstanceName: name, Status: status, APIKey: "controlled-gitee" + map[string]string{"disabled": "-disabled"}[name], Extra: string(extra)}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, model := range []struct{ name, typ, status string }{{"wrong-type", "chat", "active"}, {"disabled-parse", "doc_parse", "disabled"}} {
		details, _ := json.Marshal(map[string]any{"max_tokens": 8192, "model_types": []string{model.typ}})
		if err := dao.DB.Create(&entity.TenantModel{ID: "a410-" + model.name, ProviderID: "a410-gitee", InstanceID: "a410-gitee-one", ModelName: model.name, ModelType: model.typ, Status: model.status, Extra: string(details)}).Error; err != nil {
			t.Fatal(err)
		}
	}

	vllm := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/rerank" || r.Header.Get("Authorization") != "" {
			t.Error("keyless vLLM contract lost")
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		if body["model"] != "ranker" || body["top_n"] != float64(2) {
			t.Error("vLLM payload lost")
		}
		fmt.Fprint(w, `{"results":[{"index":1,"relevance_score":0.8},{"index":0,"relevance_score":0}]}`)
	}))
	defer vllm.Close()
	t.Log("owned-provider-listener", vllm.URL)
	if err := dao.DB.Create(&entity.TenantModelProvider{ID: "a410-vllm", TenantID: "a410-owner", ProviderName: "vllm"}).Error; err != nil {
		t.Fatal(err)
	}
	vextra, _ := json.Marshal(map[string]string{"base_url": vllm.URL, "region": "default"})
	if err := dao.DB.Create(&entity.TenantModelInstance{ID: "a410-vllm-one", ProviderID: "a410-vllm", InstanceName: "one", Status: "active", Extra: string(vextra)}).Error; err != nil {
		t.Fatal(err)
	}
	if err := dao.DB.Create(&entity.TenantModel{ID: "a410-vllm-ranker", ProviderID: "a410-vllm", InstanceID: "a410-vllm-one", ModelName: "ranker", ModelType: "rerank", Status: "active", Extra: `{"max_tokens":8192,"model_types":["rerank"]}`}).Error; err != nil {
		t.Fatal(err)
	}
	snapshot := func() string {
		var result string
		if err := readback.QueryRow(`SELECT (SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM tenant_model_provider x)||(SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM tenant_model_instance x)||(SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM tenant_model x)||(SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM t_ai_users x)||(SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM t_ai_user_tenants x)`).Scan(&result); err != nil {
			t.Fatal(err)
		}
		return result
	}
	before := snapshot()
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	app.Use(gin.Recovery())
	routes := router.NewRouter(handler.NewAuthHandler(), nil, nil, handler.NewDocumentHandler(service.NewDocumentService()), handler.NewDatasetsHandler(service.NewDatasetsService()), handler.NewSystemHandler(service.NewSystemService()), nil, nil, nil, nil, nil, nil, nil, nil, nil, handler.NewProviderHandler(nil, service.NewModelProviderService()), nil)
	routes.Setup(app)
	api := httptest.NewServer(app)
	defer api.Close()
	t.Log("owned-api-listener", api.URL)
	request := func(identity, method, path string, body any) map[string]any {
		t.Helper()
		raw, _ := json.Marshal(body)
		req, _ := http.NewRequestWithContext(t.Context(), method, api.URL+"/api/v1"+path, bytes.NewReader(raw))
		req.Header.Set("Content-Type", "application/json")
		if identity != "" {
			req.Header.Set("Authorization", "Bearer "+tokens[identity])
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var result map[string]any
		if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
			t.Fatal(err)
		}
		return result
	}
	binaryData, err := os.ReadFile(binary)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("candidate-cli-sha256 %x", sha256.Sum256(binaryData))
	parsed, _ := url.Parse(api.URL)
	runCLI := func(syntax string, success bool) string {
		t.Helper()
		p := exec.CommandContext(t.Context(), binary, "-h", parsed.Host, "-t", tokens["owner"], "-o", "json", syntax)
		out, err := p.CombinedOutput()
		if (err == nil) != success {
			t.Fatalf("CLI success=%v: %v %s", success, err, out)
		}
		t.Log("owned-cli-process-reaped", p.ProcessState.Pid(), p.ProcessState.ExitCode())
		return string(out)
	}
	t.Run("parse", func(t *testing.T) {
		file := filepath.Join(t.TempDir(), "document.pdf")
		if err := os.WriteFile(file, []byte("document bytes"), 0600); err != nil {
			t.Fatal(err)
		}
		if out := runCLI("PARSE WITH 'MinerU2.5@one@Gitee' FILE '"+file+"';", true); !strings.Contains(out, "job-1") || !strings.Contains(out, "pending") {
			t.Fatal(out)
		}
		runCLI("PARSE WITH 'MinerU2.5@one@Gitee' URL 'https://files.invalid/doc.pdf';", true)
		runCLI("LIST 'one@Gitee' TASKS;", true)
		if out := runCLI("SHOW 'one@Gitee' TASK 'job-1';", true); !strings.Contains(out, "pending") {
			t.Fatal(out)
		}
		state.Store("success")
		if out := runCLI("SHOW 'one@Gitee' TASK 'job-1';", true); !strings.Contains(out, "page two") {
			t.Fatal(out)
		}
		state.Store("failed")
		runCLI("SHOW 'one@Gitee' TASK 'job-1';", false)
		count := calls.Load()
		payload := func(name, instance string) map[string]any {
			return map[string]any{"provider_name": "Gitee", "instance_name": instance, "model_name": name, "content": base64.StdEncoding.EncodeToString([]byte("document bytes"))}
		}
		for _, identity := range []string{"", "foreign", "revoked", "disabled"} {
			if out := request(identity, "POST", "/file/parse", payload("MinerU2.5", "one")); out["code"] == float64(0) || out["data"] != nil {
				t.Fatal("parse authorization failed", out)
			}
			if out := request(identity, "GET", "/providers/Gitee/instances/one/tasks", nil); out["code"] == float64(0) || out["data"] != nil {
				t.Fatal("tasks authorization failed", out)
			}
		}
		for _, name := range []string{"wrong-type", "disabled-parse", "missing"} {
			if out := request("owner", "POST", "/file/parse", payload(name, "one")); out["code"] == float64(0) {
				t.Fatal("model denial failed", out)
			}
		}
		if out := request("owner", "POST", "/file/parse", payload("MinerU2.5", "disabled")); out["code"] == float64(0) {
			t.Fatal("instance denial failed")
		}
		if calls.Load() != count {
			t.Fatal("denial called provider")
		}
	})

	t.Run("vllm-rerank", func(t *testing.T) {
		out := runCLI("RERANK QUERY 'query' DOCUMENT 'one' 'two' WITH 'ranker@one@vllm' TOP 0;", true)
		if !strings.Contains(out, "0.8") || !strings.Contains(out, "0") {
			t.Fatal(out)
		}
		bound, err := service.NewModelProviderService().GetRerankModel("a410-owner", "ranker@one@vllm")
		if err != nil {
			t.Fatal(err)
		}
		scores, err := bound.Rerank("query", []string{"one", "two"})
		if err != nil || len(scores) != 2 || scores[0] != 0 || scores[1] != .8 {
			t.Fatal("input-order score mapping lost", scores, err)
		}
		count := calls.Load()
		if out := request("foreign", "POST", "/rerank", map[string]any{"provider_name": "vllm", "instance_name": "one", "model_name": "ranker", "query": "query", "documents": []string{"one", "two"}}); out["code"] == float64(0) {
			t.Fatal("vLLM tenant isolation lost")
		}
		if calls.Load() != count {
			t.Fatal("denial called vLLM")
		}
	})
	if after := snapshot(); after != before {
		t.Fatal("inference changed SQL state")
	}
	t.Logf("SQL-independent-readback-sha256 %x; provider-calls %d", sha256.Sum256([]byte(before)), calls.Load())
}
