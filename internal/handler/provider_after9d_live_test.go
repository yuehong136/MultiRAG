package handler_test

import (
	"bytes"
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
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/utility"
)

// An opt-in acceptance test for the Go provider line. The only external boundary
// controlled here is provider HTTP; routing, JWT, SQL, factories and CLI are real.
func TestAfter9dProvidersHTTPCLIAndSQL(t *testing.T) {
	config, binary := os.Getenv("MULTIRAG_AFTER9D_A_CONFIG"), os.Getenv("MULTIRAG_AFTER9D_A_CLI")
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
	if server.GetConfig().Database.Database != "multirag_after9d_a_20261024" {
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
		email := id + "@after9d.invalid"
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
		status, result, raw := call(method, path, "a-owner", body)
		if status != 200 || result["code"] != float64(0) {
			t.Fatalf("%s %s: %d %s", method, path, status, raw)
		}
	}
	var calls atomic.Int32
	type providerCase struct {
		name, chat, embedding string
		stream                bool
	}
	providers := []providerCase{
		{"Novita", "deepseek/deepseek-v4-pro", "baai/bge-m3", true},
		{"Jina", "jina-vlm", "", false},
		{"Anthropic", "claude-sonnet-4-5-20250929", "", false},
		{"CometAPI", "gpt-4o", "text-embedding-3-small", true},
	}
	for _, provider := range providers {
		if code, err := svc.AddModelProvider(provider.name, "a-owner"); err != nil || code != common.CodeSuccess {
			t.Fatal("register catalog provider", provider.name, code, err)
		}
		for _, instance := range []string{"one", "two"} {
			key := provider.name + "-" + instance
			fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if provider.name == "Anthropic" {
					if r.Header.Get("x-api-key") != key || r.Header.Get("anthropic-version") != "2023-06-01" {
						t.Error("native credentials crossed instances")
					}
				} else if r.Header.Get("Authorization") != "Bearer "+key {
					t.Error("credentials crossed instances")
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					w.WriteHeader(400)
					return
				}
				if strings.HasSuffix(r.URL.Path, "/embeddings") {
					if body["model"] != provider.embedding || body["dimensions"] != float64(2) {
						t.Error("embedding config lost", body)
					}
					inputs, _ := body["input"].([]any)
					if len(inputs) > 32 {
						t.Error("batch bound")
					}
					if len(inputs) > 0 && inputs[0] == "bad" {
						w.WriteHeader(503)
						fmt.Fprint(w, "private-provider-body")
						return
					}
					data := make([]map[string]any, 0, len(inputs))
					for i := len(inputs) - 1; i >= 0; i-- {
						data = append(data, map[string]any{"index": i, "embedding": []float64{float64(i), 0}})
					}
					json.NewEncoder(w).Encode(map[string]any{"object": "list", "data": data})
					return
				}
				definition := dao.GetModelProviderManager().FindProvider(provider.name)
				if r.URL.Path != "/instance/"+definition.URLSuffix.Chat || body["model"] != provider.chat {
					t.Error("configured chat endpoint/name lost", r.URL.Path, body)
				}
				messages, _ := body["messages"].([]any)
				content := ""
				if len(messages) > 0 {
					content = fmt.Sprint(messages[len(messages)-1].(map[string]any)["content"])
				}
				if strings.Contains(content, "bad") {
					if body["stream"] == true {
						w.Header().Set("Content-Type", "text/event-stream")
						fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n")
					} else {
						w.WriteHeader(503)
						fmt.Fprint(w, "private-provider-body")
					}
					return
				}
				answer := "answer-" + provider.name + "-" + instance
				if provider.name == "Anthropic" {
					json.NewEncoder(w).Encode(map[string]any{"content": []map[string]any{{"type": "text", "text": answer}}})
					return
				}
				if body["stream"] == true {
					w.Header().Set("Content-Type", "text/event-stream")
					fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":%q}}]}\n\ndata: [DONE]\n\n", answer)
				} else {
					json.NewEncoder(w).Encode(map[string]any{"choices": []map[string]any{{"message": map[string]any{"content": answer}}}})
				}
			}))
			t.Log("owned-provider-listener", provider.name, instance, fixture.URL)
			t.Cleanup(func() { after9dCloseListener(t, fixture) })
			path := "/api/v1/providers/" + url.PathEscape(provider.name) + "/instances"
			requireSuccess("POST", path, map[string]any{"instance_name": instance, "api_key": key, "region": "office", "base_url": fixture.URL + "/instance/"})
			var storedKey, storedExtra string
			if err := readback.QueryRow(`SELECT i.api_key,i.extra FROM tenant_model_instance i JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.provider_name=$1 AND i.instance_name=$2`, provider.name, instance).Scan(&storedKey, &storedExtra); err != nil {
				t.Fatal(err)
			}
			if storedKey != key || !strings.Contains(storedExtra, fixture.URL) {
				t.Fatal("independent SQL instance readback")
			}
		}
	}
	snapshot := func() string {
		t.Helper()
		var result string
		for _, table := range []string{"tenant_model_provider", "tenant_model_instance", "tenant_model", "t_ai_user_tenants", "t_ai_users"} {
			var value string
			if err := readback.QueryRow("SELECT md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY id)::text,'')) FROM " + table + " t").Scan(&value); err != nil {
				t.Fatal("independent SQL snapshot", err)
			}
			result += value
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
	runCLI := func(identity, syntax string, success bool, want string) {
		t.Helper()
		process := exec.CommandContext(t.Context(), binary, "-h", parsed.Host, "-t", tokens[identity], "-o", "json", syntax)
		output, err := process.CombinedOutput()
		t.Log("owned-cli-process", process.Process.Pid, "exit", process.ProcessState.ExitCode(), syntax)
		if (err == nil) != success || want != "" && !bytes.Contains(output, []byte(want)) || bytes.Contains(output, []byte("private-provider-body")) {
			t.Fatalf("CLI success=%v result %v: %s", success, err, output)
		}
		if snapshot() != before {
			t.Fatal("inference changed persisted state")
		}
	}
	for _, provider := range providers {
		for _, instance := range []string{"one", "two"} {
			t.Run(provider.name+"/"+instance, func(t *testing.T) {
				composite := provider.chat + "@" + instance + "@" + provider.name
				n := calls.Load()
				runCLI("a-owner", fmt.Sprintf(`CHAT WITH %q MESSAGE "q";`, composite), true, "answer-"+provider.name+"-"+instance)
				runCLI("a-owner", fmt.Sprintf(`CHAT WITH %q MESSAGE "bad";`, composite), false, "")
				if calls.Load()-n != 2 {
					t.Fatal("chat did not use bound HTTP", calls.Load()-n)
				}
				if provider.stream {
					runCLI("a-owner", fmt.Sprintf(`STREAM CHAT WITH %q MESSAGE "q";`, composite), true, "answer-"+provider.name+"-"+instance)
					runCLI("a-owner", fmt.Sprintf(`STREAM CHAT WITH %q MESSAGE "bad";`, composite), false, "")
				} else {
					n = calls.Load()
					runCLI("a-owner", fmt.Sprintf(`STREAM CHAT WITH %q MESSAGE "q";`, composite), false, "")
					if calls.Load() != n {
						t.Fatal("unsupported stream made HTTP request")
					}
				}
				if provider.embedding != "" {
					emb := provider.embedding + "@" + instance + "@" + provider.name
					n = calls.Load()
					runCLI("a-owner", fmt.Sprintf(`EMBED TEXT %s WITH %q DIMENSION 2;`, strings.Repeat("'a' ", 33), emb), true, "")
					runCLI("a-owner", fmt.Sprintf(`EMBED TEXT %s 'bad' WITH %q DIMENSION 2;`, strings.Repeat("'a' ", 32), emb), false, "")
					if calls.Load()-n != 4 {
						t.Fatal("embedding did not batch", calls.Load()-n)
					}
				}
				n = calls.Load()
				runCLI("a-other", fmt.Sprintf(`CHAT WITH %q MESSAGE "q";`, composite), false, "")
				_, denied, _ := call("POST", "/api/v1/chat/completions", "", map[string]any{"provider_name": provider.name, "instance_name": instance, "model_name": provider.chat, "message": "q"})
				if denied["code"] == float64(0) || calls.Load() != n || snapshot() != before {
					t.Fatal("denial side effects")
				}
			})
		}
	}
	t.Log("catalog -> real router/JWT -> service -> independent PostgreSQL -> bound provider HTTP; inference/denial snapshots unchanged; total controlled provider calls", calls.Load())
}

func after9dCloseListener(t *testing.T, server *httptest.Server) {
	t.Helper()
	address := server.Listener.Addr().String()
	server.Close()
	connection, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
	if err == nil {
		connection.Close()
		t.Error("owned listener remains", address)
	}
	t.Log("owned-listener-absent-readback", address)
}
