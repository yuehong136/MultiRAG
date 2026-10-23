package handler_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
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
	"multirag/internal/entity/models"
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/utility"
)

// An opt-in acceptance test for the Go provider line. The only external boundary
// controlled here is provider HTTP; routing, JWT, SQL, factories and CLI are real.
func TestAfterBBBProvidersHTTPCLIAndSQL(t *testing.T) {
	config, binary := os.Getenv("MULTIRAG_AFTERBBB_A_CONFIG"), os.Getenv("MULTIRAG_AFTERBBB_A_CLI")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned A-line scratch config required")
		}
		t.Skip("requires owned A-line scratch database")
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
	if server.GetConfig().Database.Database != "multirag_afterbbb_a" {
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
	local.InitAdminStatus(0, "owned A-line acceptance")
	active := "1"
	tokens := map[string]string{}
	for _, id := range []string{"a-owner", "a-other"} {
		email := id + "@afterbbb.invalid"
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
	defer api.Close()
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
		status, result, raw := call(method, path, "a-owner", body)
		if status != 200 || result["code"] != float64(0) {
			t.Fatalf("%s %s: %d %s", method, path, status, raw)
		}
	}
	var calls atomic.Int32
	var listeners []*httptest.Server
	t.Cleanup(func() {
		for _, s := range listeners {
			s.Close()
		}
	})
	providerNames := []string{"LocalAI", "Gitee", "Voyage AI", "ZHIPU-AI"}
	for pi, provider := range providerNames {
		if code, err := svc.AddModelProvider(provider, "a-owner"); err != nil || code != 0 {
			t.Fatal("register canonical provider", provider, code, err)
		}
		instances := []string{"one"}
		if pi < 2 {
			instances = append(instances, "two")
		}
		for _, instance := range instances {
			key := fmt.Sprintf("controlled-%d-%s", pi, instance)
			if provider == "LocalAI" && instance == "one" {
				key = ""
			}
			expected := key
			instanceName := instance
			providerName := provider
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				auth := ""
				if expected != "" {
					auth = "Bearer " + expected
				}
				if r.Header.Get("Authorization") != auth {
					t.Error("tenant credentials crossed instances")
				}
				if strings.HasSuffix(r.URL.Path, "/models") {
					io.WriteString(w, `{"data":[{"id":"running","owned_by":"vendor"}]}`)
					return
				}
				if r.URL.Path == "/images/ocr" {
					if err := r.ParseMultipartForm(1024); err != nil {
						t.Error(err)
						return
					}
					defer r.MultipartForm.RemoveAll()
					if r.FormValue("model") != "HunyuanOCR" {
						t.Error("OCR model")
					}
					if u := r.FormValue("image"); u != "" {
						if u != "https://example.com/image.png" {
							t.Error("URL changed")
						}
					} else {
						f, _, err := r.FormFile("image")
						if err != nil {
							t.Error(err)
							return
						}
						b, _ := io.ReadAll(f)
						f.Close()
						if string(b) != "image bytes" {
							t.Error("file changed")
						}
					}
					json.NewEncoder(w).Encode(map[string]any{"text_result": "识别🙂-" + instanceName})
					return
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				switch r.URL.Path {
				case "/embeddings", "/v1/embeddings":
					field := "dimensions"
					if providerName == "Voyage AI" {
						field = "output_dimension"
					}
					if body[field] != float64(2) {
						t.Error("dimension contract", body)
					}
					inputs := body["input"].([]any)
					if inputs[0] == "bad" {
						io.WriteString(w, `{"data":[{"index":null,"embedding":[0,1]}]}`)
						return
					}
					items := []map[string]any{}
					for i := len(inputs) - 1; i >= 0; i-- {
						items = append(items, map[string]any{"index": i, "embedding": []float64{float64(pi + 1), 2}})
					}
					json.NewEncoder(w).Encode(map[string]any{"data": items})
				case "/rerank", "/v1/rerank":
					field, resultField := "top_n", "results"
					if providerName == "Voyage AI" {
						field, resultField = "top_k", "data"
					}
					count := int(body[field].(float64))
					items := []map[string]any{}
					for i := 0; i < count; i++ {
						items = append(items, map[string]any{"index": i, "relevance_score": 0})
					}
					json.NewEncoder(w).Encode(map[string]any{resultField: items})
				case "/chat/completions":
					if body["stream"] == true {
						io.WriteString(w, "data: {\"choices\":[{\"delta\":{\"thinking\":\"推理🙂\",\"content\":\"答案🙂\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n")
					} else {
						io.WriteString(w, `{"choices":[{"message":{"content":"答案🙂","thinking":"推理🙂"}}]}`)
					}
				default:
					t.Error("unexpected provider endpoint", r.URL.Path)
				}
			}))
			listeners = append(listeners, upstream)
			t.Log("owned-provider-listener", provider, instance, upstream.URL)
			base := "/api/v1/providers/" + url.PathEscape(provider) + "/instances"
			requireSuccess("POST", base, map[string]any{"instance_name": instance, "api_key": key, "base_url": upstream.URL, "region": "office"})
			var storedKey, storedExtra string
			if err := readback.QueryRow(`SELECT i.api_key,i.extra FROM tenant_model_instance i JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.provider_name=$1 AND i.instance_name=$2`, provider, instance).Scan(&storedKey, &storedExtra); err != nil {
				t.Fatal(err)
			}
			if storedKey != key || !strings.Contains(storedExtra, upstream.URL) {
				t.Fatal("independent instance readback failed")
			}
			if provider == "LocalAI" {
				for model, kind := range map[string]string{"local-chat": "chat", "local-embed": "embedding", "local-rerank": "rerank", "local-no-ocr": "ocr"} {
					requireSuccess("POST", base+"/"+instance+"/models", map[string]any{"model_name": model, "model_type": kind, "max_tokens": 8192})
				}
			} else {
				var providerRow entity.TenantModelProvider
				if err := dao.DB.Where("provider_name = ?", provider).First(&providerRow).Error; err != nil {
					t.Fatal(err)
				}
				var inst entity.TenantModelInstance
				if err := dao.DB.Where("provider_id = ? AND instance_name = ?", providerRow.ID, instance).First(&inst).Error; err != nil {
					t.Fatal(err)
				}
				types := map[string]string{"HunyuanOCR": "ocr"}
				if provider == "Voyage AI" {
					types = map[string]string{"voyage-3.5": "embedding", "rerank-2": "rerank"}
				}
				if provider == "ZHIPU-AI" {
					types = map[string]string{"embedding-3": "embedding"}
				}
				for model, kind := range types {
					extra, _ := json.Marshal(map[string]any{"max_tokens": 8192, "model_types": []string{kind}})
					if err := dao.DB.Create(&entity.TenantModel{ID: fmt.Sprintf("a-%d-%s-%s", pi, instance, kind), ProviderID: providerRow.ID, InstanceID: inst.ID, ModelName: model, ModelType: kind, Status: "active", Extra: string(extra)}).Error; err != nil {
						t.Fatal(err)
					}
				}
			}
		}
	}
	snapshot := func() string {
		t.Helper()
		var value string
		if err := readback.QueryRow(`SELECT (SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM tenant_model_provider x) || (SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM tenant_model_instance x) || (SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM tenant_model x)`).Scan(&value); err != nil {
			t.Fatal(err)
		}
		return value
	}
	before := snapshot()
	binaryData, err := os.ReadFile(binary)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("candidate-cli-sha256 %x", sha256.Sum256(binaryData))
	parsed, _ := url.Parse(api.URL)
	runCLI := func(syntax string, success bool, expected string) {
		t.Helper()
		process := exec.CommandContext(t.Context(), binary, "-h", parsed.Host, "-t", tokens["a-owner"], "-o", "json", syntax)
		output, err := process.CombinedOutput()
		t.Log("owned-cli-process", process.Process.Pid, "exit", process.ProcessState.ExitCode(), syntax)
		if (err == nil) != success || (expected != "" && !bytes.Contains(output, []byte(expected))) {
			t.Fatalf("CLI result %v: %s", err, output)
		}
	}
	image := filepath.Join(t.TempDir(), "image.png")
	if err := os.WriteFile(image, []byte("image bytes"), 0600); err != nil {
		t.Fatal(err)
	}
	for _, instance := range []string{"one", "two"} {
		runCLI(fmt.Sprintf(`OCR WITH "HunyuanOCR@%s@Gitee" FILE %q;`, instance, image), true, "识别🙂-"+instance)
		runCLI(fmt.Sprintf(`OCR WITH "HunyuanOCR@%s@Gitee" URL "https://example.com/image.png";`, instance), true, "识别🙂-"+instance)
		runCLI(fmt.Sprintf(`EMBED TEXT 'a' 'b' WITH 'local-embed@%s@LocalAI' DIMENSION 2;`, instance), true, "")
		runCLI(fmt.Sprintf(`RERANK QUERY 'q' DOCUMENT 'a' 'b' WITH 'local-rerank@%s@LocalAI' TOP 1;`, instance), true, "")
		runCLI(fmt.Sprintf(`CHAT WITH "local-chat@%s@LocalAI" MESSAGE "x";`, instance), true, "答案🙂")
		runCLI(fmt.Sprintf(`STREAM THINK CHAT WITH "local-chat@%s@LocalAI" MESSAGE "x";`, instance), true, "答案🙂")
		runCLI(fmt.Sprintf(`LIST SUPPORTED MODELS FROM "LocalAI" "%s";`, instance), true, "running")
	}
	runCLI(`LIST SUPPORTED MODELS FROM "ZHIPU-AI" "one";`, true, "running@vendor")
	runCLI(`EMBED TEXT 'a' 'b' WITH 'voyage-3.5@one@Voyage AI' DIMENSION 2;`, true, "")
	runCLI(`RERANK QUERY 'q' DOCUMENT 'a' 'b' WITH 'rerank-2@one@Voyage AI' TOP 1;`, true, "")
	runCLI(`LIST SUPPORTED MODELS FROM "Voyage AI" "one";`, false, "")
	runCLI(`CHECK INSTANCE "one" FROM "LocalAI";`, true, "")
	beforeUnsupported := calls.Load()
	runCLI(`CHECK INSTANCE "one" FROM "Voyage AI";`, false, "")
	if calls.Load() != beforeUnsupported {
		t.Fatal("Voyage connection probe consumed quota")
	}
	beforeDenied := calls.Load()
	_, unsupported, rawUnsupported := call("POST", "/api/v1/file/ocr", "a-owner", map[string]any{"provider_name": "LocalAI", "instance_name": "one", "model_name": "local-no-ocr", "url": "https://example.com/image.png"})
	if unsupported["code"] != float64(common.CodeNotImplemented) {
		t.Fatal("missing capability must return 501", rawUnsupported)
	}
	for _, test := range []struct {
		user string
		body map[string]any
	}{
		{"", map[string]any{"provider_name": "Gitee", "instance_name": "one", "model_name": "HunyuanOCR", "content": []byte("x")}},
		{"a-other", map[string]any{"provider_name": "Gitee", "instance_name": "one", "model_name": "HunyuanOCR", "content": []byte("x")}},
		{"a-owner", map[string]any{"provider_name": "Gitee", "instance_name": "one", "model_name": "HunyuanOCR", "content": []byte("x"), "url": "https://example.com/image.png"}},
		{"a-owner", map[string]any{"provider_name": "Gitee", "instance_name": "one", "model_name": "HunyuanOCR", "url": "file:///tmp/a"}},
		{"a-owner", map[string]any{"provider_name": "LocalAI", "instance_name": "one", "model_name": "local-chat", "content": []byte("x")}},
		{"a-owner", map[string]any{"provider_name": "LocalAI", "instance_name": "one", "model_name": "local-no-ocr", "url": "https://example.com/image.png"}},
	} {
		_, result, raw := call("POST", "/api/v1/file/ocr", test.user, test.body)
		if result["code"] == float64(0) {
			t.Fatal("denial success", raw)
		}
	}
	runCLI(`OCR WITH "local-no-ocr@one@LocalAI" FILE `+fmt.Sprintf("%q;", image), false, "does not support")
	if calls.Load() != beforeDenied {
		t.Fatal("denied request made provider call")
	}
	runCLI(`EMBED TEXT 'bad' WITH 'local-embed@one@LocalAI' DIMENSION 2;`, false, "")
	// Independently prove disabled instances make no outbound requests.
	if err := dao.DB.Exec(`UPDATE tenant_model_instance SET status='disabled' WHERE id IN (SELECT i.id FROM tenant_model_instance i JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.provider_name='Gitee' AND i.instance_name='two')`).Error; err != nil {
		t.Fatal(err)
	}
	beforeDisabled := calls.Load()
	runCLI(`OCR WITH "HunyuanOCR@two@Gitee" URL "https://example.com/image.png";`, false, "")
	if calls.Load() != beforeDisabled {
		t.Fatal("disabled instance called provider")
	}
	if err := dao.DB.Exec(`UPDATE tenant_model_instance SET status='active' WHERE status='disabled'`).Error; err != nil {
		t.Fatal(err)
	}
	if snapshot() != before {
		t.Fatal("inference or denial mutated provider state")
	}
	// Call cancellation uses the incoming request context (driver coverage also
	// checks mid-stream cancellation and a real stalled response).
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if _, code, err := svc.OCRURL("Gitee", "one", "HunyuanOCR", "a-owner", "https://example.com/image.png", &models.APIConfig{Context: ctx}, nil); err == nil || code == 0 {
		t.Fatal("cancel ignored")
	}
	if err := os.Remove(image); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(image); !os.IsNotExist(err) {
		t.Fatal("private image survived cleanup")
	}
	api.Close()
	for _, s := range listeners {
		s.Close()
	}
	for _, s := range append(listeners, api) {
		u, _ := url.Parse(s.URL)
		connection, err := net.DialTimeout("tcp", u.Host, 200*time.Millisecond)
		if err == nil {
			connection.Close()
			t.Fatal("owned listener survived")
		}
		t.Log("owned-listener-closed-readback", s.URL)
	}
	t.Logf("real CLI/JWT/router/SQL/provider HTTP accepted; provider calls=%d; independent SQL state unchanged; denied/disabled calls zero; image/listeners cleaned", calls.Load())
}
