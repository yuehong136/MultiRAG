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
	"github.com/google/uuid"
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
	for _, id := range []string{"owner", "member", "foreign", "revoked", "disabled"} {
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
		tenant, role := "a410-"+id, "owner"
		if id == "member" {
			tenant, role = "a410-owner", "normal"
		}
		if err := dao.DB.Create(&entity.UserTenant{ID: "a410-" + id, UserID: "a410-" + id, TenantID: tenant, Role: role, InvitedBy: "a410-" + id, Status: &relation}).Error; err != nil {
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

	active := "1"
	tenantName := "owned fixture"
	if err := dao.DB.Create(&entity.Tenant{ID: "a410-owner", Name: &tenantName, Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	datasetIDs := map[string]string{}
	t.Cleanup(func() {
		if err := dao.DB.Where("id LIKE ?", "a410-%").Delete(&entity.Document{}).Error; err != nil {
			t.Error(err)
		}
		if err := dao.DB.Where("tenant_id LIKE ?", "a410-%").Delete(&entity.Knowledgebase{}).Error; err != nil {
			t.Error(err)
		}
		if err := dao.DB.Where("id = ?", "a410-owner").Delete(&entity.Tenant{}).Error; err != nil {
			t.Error(err)
		}
	})
	for _, kind := range []string{"private", "shared", "empty", "foreign", "disabled"} {
		id, err := uuid.NewUUID()
		if err != nil {
			t.Fatal(err)
		}
		datasetIDs[kind] = strings.ReplaceAll(id.String(), "-", "")
		tenant, permission, status := "a410-owner", "me", "1"
		if kind == "shared" {
			permission = "team"
		}
		if kind == "foreign" {
			tenant = "a410-foreign"
		}
		if kind == "disabled" {
			status = "0"
		}
		if err := dao.DB.Create(&entity.Knowledgebase{ID: datasetIDs[kind], TenantID: tenant, CreatedBy: tenant, Name: "a410-" + kind, Permission: permission, EmbdID: "unconfigured", Status: &status}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for i := range 3 {
		name := fmt.Sprintf("document-%d", i)
		meta := entity.JSONMap{"source": "approved"}
		status := "1"
		if i == 2 {
			status = "0"
		}
		if err := dao.DB.Create(&entity.Document{ID: fmt.Sprintf("a410-doc-%d", i), KbID: datasetIDs["shared"], Name: &name, Type: "txt", Suffix: "txt", CreatedBy: "a410-owner", Status: &status, ParserConfig: entity.JSONMap{}, MetaFields: &meta}).Error; err != nil {
			t.Fatal(err)
		}
	}

	for pi, providerName := range []string{"SiliconFlow", "StepFun"} {
		providerID := fmt.Sprintf("a410-speech-%d", pi)
		if err := dao.DB.Create(&entity.TenantModelProvider{ID: providerID, TenantID: "a410-owner", ProviderName: providerName}).Error; err != nil {
			t.Fatal(err)
		}
		for _, instance := range []string{"one", "two"} {
			key := providerName + "-" + instance
			fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.Header.Get("Authorization") != "Bearer "+key {
					t.Error("speech instance credentials crossed")
				}
				if strings.HasSuffix(r.URL.Path, "/transcriptions") {
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
					if string(raw) != "audio bytes" || r.FormValue("model") != "FunAudioLLM/SenseVoiceSmall" || r.FormValue("language") != "en" {
						t.Error("ASR consumer bytes/fields lost")
					}
					fmt.Fprint(w, `{"text":"transcription"}`)
					return
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				if body["input"] == "provider-error" {
					w.WriteHeader(502)
					fmt.Fprint(w, "private-provider-body")
					return
				}
				if body["voice"] != "selected" || body["response_format"] != "wav" {
					t.Error("TTS consumer params lost")
				}
				audio := []byte("speech-" + key + "\x00\xff")
				if body["stream_format"] == "sse" {
					w.Header().Set("Content-Type", "text/event-stream")
					fmt.Fprintf(w, "data: {\"type\":\"speech.audio.delta\",\"audio\":%q}\n\n", base64.StdEncoding.EncodeToString(audio))
					if body["input"] != "missing-final" {
						fmt.Fprint(w, "data: {\"type\":\"speech.audio.done\"}\n\n")
					}
				} else {
					w.Header().Set("Content-Type", "audio/wav")
					w.Write(audio)
				}
			}))
			t.Cleanup(fixture.Close)
			t.Log("owned-provider-listener", fixture.URL)
			extra, _ := json.Marshal(map[string]string{"base_url": fixture.URL, "region": "default"})
			if err := dao.DB.Create(&entity.TenantModelInstance{ID: fmt.Sprintf("a410-speech-%d-%s", pi, instance), ProviderID: providerID, InstanceName: instance, Status: "active", APIKey: key, Extra: string(extra)}).Error; err != nil {
				t.Fatal(err)
			}
		}
	}
	snapshot := func() string {
		var result string
		if err := readback.QueryRow(`SELECT (SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM tenant_model_provider x)||(SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM tenant_model_instance x)||(SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM tenant_model x)||(SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM t_ai_users x)||(SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM t_ai_user_tenants x)||(SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM t_ai_knowledgebases x)||(SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM t_ai_documents x)`).Scan(&result); err != nil {
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

	t.Run("dataset-documents", func(t *testing.T) {
		if out := runCLI("LIST DOCUMENTS FROM 'a410-shared';", true); !strings.Contains(out, "approved") || !strings.Contains(out, `"total": 2`) {
			t.Fatal(out)
		}
		runCLI("LIST DOCUMENTS FROM '"+datasetIDs["shared"]+"';", true)
		for _, identity := range []string{"owner", "member"} {
			out := request(identity, "GET", "/datasets/"+datasetIDs["shared"]+"/documents?page=2&page_size=1", nil)
			if out["code"] != float64(0) {
				t.Fatal("shared read failed", out)
			}
			data := out["data"].(map[string]any)
			if data["total"] != float64(2) || len(data["docs"].([]any)) != 1 {
				t.Fatal("pagination failed", data)
			}
		}
		for _, identity := range []string{"", "foreign", "revoked", "disabled"} {
			if out := request(identity, "GET", "/datasets/"+datasetIDs["shared"]+"/documents", nil); out["code"] == float64(0) || out["data"] != nil {
				t.Fatal("dataset denial failed", out)
			}
		}
		for _, kind := range []string{"private", "foreign", "disabled"} {
			if out := request("member", "GET", "/datasets/"+datasetIDs[kind]+"/documents", nil); out["code"] == float64(0) {
				t.Fatal("private/foreign/disabled read leaked", out)
			}
		}
		for _, query := range []string{"?page=0", "?page=-1", "?page_size=101", "?page=nope", "?return_empty_metadata=bad"} {
			if out := request("owner", "GET", "/datasets/"+datasetIDs["shared"]+"/documents"+query, nil); out["code"] != float64(400) {
				t.Fatal("invalid query accepted", out)
			}
		}
		if out := request("owner", "GET", "/datasets/"+datasetIDs["empty"]+"/documents", nil); out["code"] != float64(0) || len(out["data"].(map[string]any)["docs"].([]any)) != 0 {
			t.Fatal("empty list failed", out)
		}
	})

	t.Run("speech-providers", func(t *testing.T) {
		for _, providerName := range []string{"SiliconFlow", "StepFun"} {
			model := "fnlp/MOSS-TTSD-v0.5"
			if providerName == "StepFun" {
				model = "step-tts-2"
			}
			for _, instance := range []string{"one", "two"} {
				for _, stream := range []string{"", "STREAM "} {
					output := runCLI(stream+"TTS WITH '"+model+"@"+instance+"@"+providerName+"' TEXT 'hello' FORMAT 'wav' PARAM '{\"voice\":\"selected\"}' SAVE '"+t.TempDir()+"';", true)
					var rows []map[string]any
					if err := json.Unmarshal([]byte(output), &rows); err != nil || len(rows) != 1 {
						t.Fatal("bad speech CLI output", output)
					}
					path, _ := rows[0]["saved_path"].(string)
					audio, err := os.ReadFile(path)
					if err != nil || string(audio) != "speech-"+providerName+"-"+instance+"\x00\xff" {
						t.Fatal("binary speech output lost", path, err)
					}
				}
			}
			out := request("owner", "POST", "/audio/speech", map[string]any{"provider_name": providerName, "instance_name": "one", "model_name": model, "text": "provider-error", "tts_config": map[string]any{"voice": "selected", "format": "wav"}})
			if out["code"] == float64(0) || strings.Contains(fmt.Sprint(out), "private-provider-body") {
				t.Fatal("provider failure accepted/leaked", out)
			}
		}
		file := filepath.Join(t.TempDir(), "input.wav")
		if err := os.WriteFile(file, []byte("audio bytes"), 0600); err != nil {
			t.Fatal(err)
		}
		if out := runCLI("ASR WITH 'FunAudioLLM/SenseVoiceSmall@one@SiliconFlow' AUDIO '"+file+"' PARAM '{\"language\":\"en\"}';", true); !strings.Contains(out, "transcription") {
			t.Fatal(out)
		}
		runCLI("STREAM TTS WITH 'step-tts-2@one@StepFun' TEXT 'missing-final' FORMAT 'wav' PARAM '{\"voice\":\"selected\"}';", false)
		count := calls.Load()
		for _, identity := range []string{"", "foreign", "revoked", "disabled"} {
			if out := request(identity, "POST", "/audio/speech", map[string]any{"provider_name": "SiliconFlow", "instance_name": "one", "model_name": "fnlp/MOSS-TTSD-v0.5", "text": "hello"}); out["code"] == float64(0) || out["data"] != nil {
				t.Fatal("speech denial failed", out)
			}
		}
		if calls.Load() != count {
			t.Fatal("speech denial called provider")
		}
	})
	if after := snapshot(); after != before {
		t.Fatal("inference changed SQL state")
	}
	t.Logf("SQL-independent-readback-sha256 %x; provider-calls %d", sha256.Sum256([]byte(before)), calls.Load())
}
