package handler_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"fmt"
	"github.com/aws/aws-sdk-go-v2/aws/protocol/eventstream"
	"io"
	"net/http"
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
func TestAfter4776ProvidersHTTPCLIAndSQL(t *testing.T) {
	config, cliBinary := os.Getenv("MULTIRAG_AFTER4776_G_CONFIG"), os.Getenv("MULTIRAG_AFTER4776_G_CLI")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned after4776 G PostgreSQL config required")
		}
		t.Skip("requires owned after4776 G PostgreSQL config and candidate CLI")
	}
	if cliBinary == "" {
		t.Fatal("candidate CLI required")
	}
	projectRoot := filepath.Join("..", "..")
	if _, err := os.Stat("go.mod"); err == nil {
		projectRoot = "."
	}
	t.Chdir(projectRoot)
	t.Setenv("MULTIRAG_AFTER775_D_RESOURCES", os.Getenv("MULTIRAG_AFTER4776_G_RESOURCES"))
	t.Setenv("AWS_EC2_METADATA_DISABLED", "true")
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal("private config load failed")
	}
	if db := server.GetConfig().Database; db.Database != "multirag_after4776_g_20261025" || db.Host != "127.0.0.1" {
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
	tables := []string{"tenant_model", "tenant_model_instance", "tenant_model_provider", "t_ai_tenant_llms", "t_ai_user_tenants", "t_ai_users"}
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
		t.Log("owned PostgreSQL cleanup independently read back zero rows in six tables")
	})
	if err := server.InitVariables(statusSigningStore{secret: strings.Repeat("owned-after4776-", 4)}); err != nil {
		t.Fatal(err)
	}
	local.InitAdminStatus(0, "owned after4776 provider acceptance")
	active := "1"
	tokens := map[string]string{}
	for _, id := range []string{"after4776-g-owner", "after4776-g-other"} {
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
	api := after775Server(t, "application", app)

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
		status, envelope, raw := request(method, path, "after4776-g-owner", body)
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
	runCLI := func(t *testing.T, identity, syntax string, valid bool, want string) []byte {
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

	var calls atomic.Int64
	type providerCase struct{ name, chat, embed, rerank, ocr string }
	providers := []providerCase{
		{name: "Bedrock", chat: "anthropic.claude-3-5-sonnet-20241022-v2:0"},
		{name: "FuturMix", chat: "gpt-5.5"},
		{name: "ollama", chat: "owned-chat", embed: "owned-embed"},
		{name: "DeepInfra", rerank: "owned/rerank"},
		{name: "ZHIPU-AI", ocr: "owned-ocr"},
		{name: "Baidu", ocr: "owned-ocr"},
	}
	image := []byte("owned-image-content")
	mediaDir := filepath.Join(filepath.Dir(config), "imports")
	if err := os.MkdirAll(mediaDir, 0700); err != nil {
		t.Fatal(err)
	}
	imageFile := filepath.Join(mediaDir, "provider-image.bin")
	if err := os.WriteFile(imageFile, image, 0600); err != nil {
		t.Fatal(err)
	}
	for _, p := range providers {
		success("PUT", "/api/v1/providers/", map[string]any{"provider_name": p.name})
		definition := dao.GetModelProviderManager().FindProvider(p.name)
		if definition == nil {
			t.Fatal("catalog missing", p.name)
		}
		instances := []string{"one"}
		if p.name == "Bedrock" || p.name == "FuturMix" {
			instances = append(instances, "two")
		}
		for _, instance := range instances {
			key := "controlled-" + p.name + "-" + instance
			region := "us-east-1"
			if instance == "two" {
				region = "eu-west-1"
			}
			if p.name == "Bedrock" {
				key = fmt.Sprintf(`{"auth_mode":"access_key_secret","bedrock_ak":%q,"bedrock_sk":"controlled-secret","bedrock_session_token":%q,"bedrock_region":%q}`, "AKID"+instance, "session-"+instance, region)
			}
			fixture := after775Server(t, p.name+"/"+instance, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if p.name == "Bedrock" {
					if !strings.Contains(r.Header.Get("Authorization"), "Credential=AKID"+instance+"/") || !strings.Contains(r.Header.Get("Authorization"), "/"+region+"/bedrock/aws4_request") || r.Header.Get("X-Amz-Security-Token") != "session-"+instance {
						t.Error("signed instance/region lost", r.Header.Get("Authorization"))
					}
					if r.Method == "GET" {
						if r.URL.Path != "/foundation-models" {
							t.Error(r.URL.Path)
						}
						fmt.Fprintf(w, `{"modelSummaries":[{"modelId":%q}]}`, p.chat)
						return
					}
				} else if p.name != "ollama" && r.Header.Get("Authorization") != "Bearer "+key {
					t.Error("credentials crossed instances", p.name, instance)
				}
				if p.name == "ollama" && r.Method == "GET" {
					fmt.Fprintf(w, `{"models":[{"name":%q}]}`, p.chat)
					return
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				switch {
				case p.name == "Bedrock":
					messages, _ := body["messages"].([]any)
					if len(messages) == 0 {
						t.Error("Bedrock messages missing")
					}
					bad := strings.Contains(fmt.Sprint(body), "bad")
					if strings.HasSuffix(r.URL.Path, "converse-stream") {
						w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
						events := [][2]string{{"messageStart", `{"role":"assistant"}`}, {"contentBlockDelta", fmt.Sprintf(`{"contentBlockIndex":0,"delta":{"text":%q}}`, "answer-Bedrock-"+instance)}, {"contentBlockStop", `{"contentBlockIndex":0}`}, {"messageStop", `{"stopReason":"end_turn"}`}}
						if bad {
							events = events[:3]
						}
						for _, event := range events {
							if err := eventstream.NewEncoder().Encode(w, eventstream.Message{Headers: eventstream.Headers{{Name: ":message-type", Value: eventstream.StringValue("event")}, {Name: ":event-type", Value: eventstream.StringValue(event[0])}}, Payload: []byte(event[1])}); err != nil {
								t.Error(err)
							}
						}
					} else {
						if bad {
							fmt.Fprint(w, `{"output":{},"stopReason":"end_turn"}`)
							return
						}
						fmt.Fprintf(w, `{"output":{"message":{"role":"assistant","content":[{"text":%q}]}},"stopReason":"end_turn"}`, "answer-Bedrock-"+instance)
					}
				case p.name == "ollama" && strings.HasSuffix(r.URL.Path, "/api/embed"):
					if body["model"] != p.embed || body["dimensions"] != float64(2) {
						t.Error("native embed model/options lost", body)
					}
					if strings.Contains(fmt.Sprint(body["input"]), "bad") {
						fmt.Fprint(w, `{"embeddings":[[null,1]]}`)
						return
					}
					inputs, _ := body["input"].([]any)
					vectors := make([][]float64, len(inputs))
					for i := range vectors {
						vectors[i] = []float64{0, 1}
					}
					json.NewEncoder(w).Encode(map[string]any{"embeddings": vectors})
				case p.name == "ollama":
					if r.URL.Path != "/api/chat" || body["model"] != p.chat {
						t.Error(r.URL.Path, body)
					}
					answer := "answer-ollama-one"
					if strings.Contains(fmt.Sprint(body["messages"]), "bad") {
						fmt.Fprint(w, `{"message":{},"done":true}`)
						return
					}
					if body["stream"] == true {
						w.Header().Set("Content-Type", "application/x-ndjson")
					}
					fmt.Fprintf(w, `{"message":{"role":"assistant","content":%q},"done":true}`, answer)
				case p.name == "DeepInfra":
					if r.URL.Path != "/v1/inference/owned/rerank" || body["query"] == nil {
						t.Error(r.URL.Path, body)
					}
					if body["query"] == "bad" {
						fmt.Fprint(w, `{"scores":[null,0]}`)
					} else {
						fmt.Fprint(w, `{"scores":[0,0.9]}`)
					}
				case p.ocr != "":
					if body["model"] != p.ocr || body["file"] == nil {
						t.Error("OCR payload missing", body)
					}
					if p.name == "ZHIPU-AI" {
						fmt.Fprint(w, `{"md_results":"recognized-ZHIPU-AI"}`)
					} else {
						fmt.Fprint(w, `{"result":{"layoutParsingResults":[{"markdown":{"text":"recognized-Baidu"}}]}}`)
					}
				default:
					if body["model"] != p.chat || r.URL.Path != "/v1/chat/completions" {
						t.Error("chat binding", r.URL.Path, body)
					}
					answer := "answer-" + p.name + "-" + instance
					if strings.Contains(fmt.Sprint(body["messages"]), "bad") {
						if body["stream"] == true {
							w.Header().Set("Content-Type", "text/event-stream")
							fmt.Fprint(w, "data: invalid\n\n")
						} else {
							w.WriteHeader(503)
							fmt.Fprint(w, "private-provider-body")
						}
						return
					}
					if body["stream"] == true {
						w.Header().Set("Content-Type", "text/event-stream")
						fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":%q},\"finish_reason\":\"stop\"}]}\n\n", answer)
					} else {
						fmt.Fprintf(w, `{"choices":[{"message":{"content":%q}}]}`, answer)
					}
				}
			}))
			runCLI(t, "after4776-g-owner", fmt.Sprintf(`CREATE PROVIDER %q INSTANCE %q KEY '%s' URL %q;`, p.name, instance, key, fixture.URL), true, "")
			var stored, extra string
			if err := readback.QueryRow(`SELECT i.api_key,i.extra FROM tenant_model_instance i JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.provider_name=$1 AND i.instance_name=$2 AND p.tenant_id='after4776-g-owner-tenant'`, p.name, instance).Scan(&stored, &extra); err != nil || stored != key || !strings.Contains(extra, fixture.URL) {
				t.Fatal("instance SQL readback", p.name, instance, err)
			}
			for name, typ := range map[string]string{p.embed: "embedding", p.rerank: "rerank", p.ocr: "ocr"} {
				if name != "" {
					success("POST", "/api/v1/providers/"+url.PathEscape(p.name)+"/instances/"+instance+"/models", map[string]any{"model_name": name, "model_type": typ, "max_tokens": 8192})
				}
			}
			if p.name == "ollama" {
				success("POST", "/api/v1/providers/ollama/instances/one/models", map[string]any{"model_name": p.chat, "model_type": "chat", "max_tokens": 8192})
			}
		}
	}
	// Admission without a catalog URL must not make a paid probe or require a fake URL.
	nativeKey := `{"auth_mode":"access_key_secret","bedrock_ak":"controlled-native","bedrock_sk":"controlled-secret","bedrock_region":"us-east-1"}`
	count := calls.Load()
	success("POST", "/api/v1/providers/Bedrock/instances", map[string]any{"instance_name": "native", "api_key": nativeKey})
	if _, err := svc.GetChatModel("after4776-g-owner-tenant", providers[0].chat+"@native@Bedrock"); err != nil {
		t.Fatal("native Bedrock service admission", err)
	}
	if count != calls.Load() {
		t.Fatal("instance admission probed a provider")
	}
	legacyName, legacyType := providers[0].chat, "chat"
	if err := dao.DB.Create(&entity.TenantLLM{TenantID: "after4776-g-owner-tenant", LLMFactory: "Bedrock", LLMName: &legacyName, ModelType: &legacyType, APIKey: &nativeKey, MaxTokens: 8192, Status: "1"}).Error; err != nil {
		t.Fatal(err)
	}
	if _, err := svc.GetChatModel("after4776-g-owner-tenant", legacyName+"@Bedrock"); err != nil {
		t.Fatal("legacy Bedrock regional binding", err)
	}

	before = snapshot()
	for _, p := range providers {
		instances := []string{"one"}
		if p.name == "Bedrock" || p.name == "FuturMix" {
			instances = append(instances, "two")
		}
		for _, instance := range instances {
			t.Run(p.name+"/"+instance, func(t *testing.T) {
				composite := func(name string) string { return name + "@" + instance + "@" + p.name }
				if p.name == "Bedrock" {
					bound, err := svc.GetChatModel("after4776-g-owner-tenant", composite(p.chat))
					if err != nil {
						t.Fatal("bound Bedrock", err)
					}
					t.Logf("Bedrock selected region %s", *bound.APIConfig.Region)
					response, err := bound.Chat("", []map[string]string{{"role": "user", "content": "q"}}, nil)
					if err != nil || response != "answer-Bedrock-"+instance {
						t.Fatal("Bedrock real service facade", response, err)
					}
				}
				if p.chat != "" {
					for _, prefix := range []string{"", "STREAM "} {
						runCLI(t, "after4776-g-owner", fmt.Sprintf(`%sCHAT WITH %q MESSAGE 'q';`, prefix, composite(p.chat)), true, "answer-"+p.name+"-"+instance)
						runCLI(t, "after4776-g-owner", fmt.Sprintf(`%sCHAT WITH %q MESSAGE 'bad';`, prefix, composite(p.chat)), false, "")
					}
				}
				if p.name == "FuturMix" {
					count := calls.Load()
					runCLI(t, "after4776-g-owner", fmt.Sprintf(`LIST MODELS FROM 'FuturMix' %q;`, instance), true, p.chat)
					runCLI(t, "after4776-g-owner", fmt.Sprintf(`LIST SUPPORTED MODELS FROM 'FuturMix' %q;`, instance), false, "")
					runCLI(t, "after4776-g-owner", fmt.Sprintf(`CHECK INSTANCE %q FROM 'FuturMix';`, instance), false, "")
					if _, err := svc.ListSupportedModels("FuturMix", instance, "after4776-g-owner"); err == nil || !strings.Contains(err.Error(), "unsupported") {
						t.Fatal("dynamic discovery must be explicit unsupported", err)
					}
					if _, err := svc.CheckProviderConnection("FuturMix", instance, "after4776-g-owner"); err == nil || !strings.Contains(err.Error(), "unsupported") {
						t.Fatal("connection check must be explicit unsupported", err)
					}
					if count != calls.Load() {
						t.Fatal("static catalog/check made a paid probe")
					}
				}
				if p.name == "Bedrock" || p.name == "ollama" {
					runCLI(t, "after4776-g-owner", fmt.Sprintf(`LIST SUPPORTED MODELS FROM %q %q;`, p.name, instance), true, p.chat)
				}
				if p.embed != "" {
					b := map[string]any{"provider_name": p.name, "instance_name": instance, "model_name": p.embed, "texts": []string{"a", "b"}, "dimension": 2}
					rows := success("POST", "/api/v1/embeddings", b)["data"].([]any)
					if len(rows) != 2 || fmt.Sprint(rows[0].(map[string]any)["embedding"]) != "[0 1]" {
						t.Fatal("native vectors lost", rows)
					}
					runCLI(t, "after4776-g-owner", fmt.Sprintf(`EMBED TEXT 'a' WITH %q DIMENSION 2;`, composite(p.embed)), true, "")
					b["texts"] = []string{"bad"}
					_, e, _ := request("POST", "/api/v1/embeddings", "after4776-g-owner", b)
					if e["code"] == float64(0) {
						t.Fatal("invalid native vector succeeded")
					}
				}
				if p.rerank != "" {
					b := map[string]any{"provider_name": p.name, "instance_name": instance, "model_name": p.rerank, "query": "q", "documents": []string{"a", "b"}}
					rows := success("POST", "/api/v1/rerank", b)["data"].([]any)
					if len(rows) != 2 || rows[1].(map[string]any)["relevance_score"] != float64(0) {
						t.Fatal("legal zero lost", rows)
					}
					runCLI(t, "after4776-g-owner", fmt.Sprintf(`RERANK QUERY 'q' DOCUMENT 'a' 'b' WITH %q TOP 2;`, composite(p.rerank)), true, "")
					b["query"] = "bad"
					_, e, _ := request("POST", "/api/v1/rerank", "after4776-g-owner", b)
					if e["code"] == float64(0) {
						t.Fatal("invalid raw score succeeded")
					}
				}
				if p.ocr != "" {
					runCLI(t, "after4776-g-owner", fmt.Sprintf(`OCR WITH %q FILE %q;`, composite(p.ocr), imageFile), true, "recognized-"+p.name)
					count := calls.Load()
					b := map[string]any{"provider_name": p.name, "instance_name": instance, "model_name": p.ocr, "content": ""}
					_, e, _ := request("POST", "/api/v1/file/ocr", "after4776-g-owner", b)
					if e["code"] == float64(0) || calls.Load() != count {
						t.Fatal("empty OCR reached provider", e)
					}
				}
				count := calls.Load()
				if p.chat != "" {
					runCLI(t, "after4776-g-other", fmt.Sprintf(`CHAT WITH %q MESSAGE 'q';`, composite(p.chat)), false, "")
				}
				if count != calls.Load() || snapshot() != before {
					t.Fatal("denial/inference changed persisted data")
				}
			})
		}
	}
	t.Logf("controlled provider calls: %d; no AWS/IMDS/paid endpoints", calls.Load())
}
