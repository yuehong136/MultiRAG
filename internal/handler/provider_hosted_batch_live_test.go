package handler_test

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/cli"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/server"
	"multirag/internal/service"
)

// Controlled identity and provider edges; real registered handlers, CLI, SQL and
// HTTP. This does not claim production auth or real cloud-provider acceptance.
func TestHostedProviderRoutesScratchPostgres(t *testing.T) {
	config := os.Getenv("MULTIRAG_HOSTED_PROVIDER_CONFIG")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned config required")
		}
		t.Skip("requires owned hosted-provider config")
	}
	t.Chdir(filepath.Join("..", ".."))
	if e := common.Init("error"); e != nil {
		t.Fatal(e)
	}
	if e := server.FromConfigFile(config); e != nil {
		t.Fatal("private config invalid")
	}
	if !strings.HasPrefix(server.GetConfig().Database.Database, "multirag_go_models_") {
		t.Fatal("unowned DB")
	}
	if e := dao.InitDB(); e != nil {
		t.Fatal("owned DB startup failed", e)
	}
	pool, e := dao.DB.DB()
	if e != nil {
		t.Fatal(e)
	}
	defer pool.Close()
	dsn, e := dao.PostgresDSN(server.GetConfig().Database)
	if e != nil {
		t.Fatal("readback DSN")
	}
	readback, e := sql.Open("pgx", dsn)
	if e != nil {
		t.Fatal("readback connection")
	}
	defer readback.Close()
	port, _ := strconv.Atoi(os.Getenv("MULTIRAG_HOSTED_TEST_PORT_BASE"))
	listen := func(h http.Handler) *httptest.Server {
		address := "127.0.0.1:0"
		if port > 0 {
			address = fmt.Sprintf("127.0.0.1:%d", port)
			port++
		}
		l, e := net.Listen("tcp", address)
		if e != nil {
			t.Fatal(e)
		}
		s := httptest.NewUnstartedServer(h)
		s.Listener.Close()
		s.Listener = l
		s.Start()
		t.Cleanup(s.Close)
		t.Log("owned listener", l.Addr().String())
		return s
	}
	active := "1"
	for _, id := range []string{"hosted-owner", "hosted-other"} {
		if e := dao.DB.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: id, Role: "owner", InvitedBy: id, Status: &active}).Error; e != nil {
			t.Fatal(e)
		}
	}
	var calls atomic.Int32
	manager := dao.GetModelProviderManager()
	for _, p := range []string{"Mistral", "Jina", "Novita", "LongCat"} {
		provider := manager.FindProvider(p)
		if provider == nil {
			t.Fatal("catalog missing")
		}
		if e := dao.DB.Create(&entity.TenantModelProvider{ID: p, TenantID: "hosted-owner", ProviderName: p}).Error; e != nil {
			t.Fatal(e)
		}
		for i := 0; i < 2; i++ {
			key := p + strconv.Itoa(i)
			fixture := listen(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.Header.Get("Authorization") != "Bearer "+key {
					t.Error("credential isolation")
				}
				var b map[string]any
				if e := json.NewDecoder(r.Body).Decode(&b); e != nil {
					t.Error(e)
				}
				switch {
				case strings.HasSuffix(r.URL.Path, "embeddings"):
					if p == "Mistral" && (b["output_dimension"] != float64(2) || b["dimensions"] != nil) {
						t.Error("Mistral native dimension missing", b)
					}
					data := []any{}
					for j := range b["input"].([]any) {
						data = append(data, map[string]any{"index": j, "embedding": []float64{0, float64(i + 1)}})
					}
					json.NewEncoder(w).Encode(map[string]any{"data": data})
				case strings.HasSuffix(r.URL.Path, "rerank"):
					fmt.Fprint(w, `{"results":[{"index":1,"relevance_score":1},{"index":0,"relevance_score":0}]}`)
				case strings.HasSuffix(r.URL.Path, "chat/completions"):
					if b["stream"] == true {
						fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: [DONE]\n\n")
					} else {
						fmt.Fprint(w, `{"choices":[{"message":{"content":"answer"}}]}`)
					}
				default:
					t.Error("endpoint", r.URL.Path)
					w.WriteHeader(404)
				}
			}))
			extra, _ := json.Marshal(map[string]string{"region": "office", "base_url": fixture.URL + "/instance"})
			if e := dao.DB.Create(&entity.TenantModelInstance{ID: key, ProviderID: p, InstanceName: strconv.Itoa(i), APIKey: key, Status: "active", Extra: string(extra)}).Error; e != nil {
				t.Fatal(e)
			}
		}
	}
	svc := service.NewModelProviderService()
	h := handler.NewProviderHandler(nil, svc)
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	router.Use(func(c *gin.Context) {
		id := strings.TrimPrefix(c.GetHeader("Authorization"), "Bearer ")
		if id == "hosted-owner" || id == "hosted-other" {
			c.Set("user", &entity.User{ID: id})
		}
		c.Next()
	})
	router.POST("/api/v1/chat/completions", h.ChatToModel)
	router.POST("/api/v1/embeddings", h.EmbedText)
	router.POST("/api/v1/rerank", h.RerankDocument)
	api := listen(router)
	request := func(path, identity string, body map[string]any, valid bool) string {
		data, _ := json.Marshal(body)
		req, _ := http.NewRequest("POST", api.URL+path, bytes.NewReader(data))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+identity)
		resp, e := http.DefaultClient.Do(req)
		if e != nil {
			t.Fatal(e)
		}
		defer resp.Body.Close()
		data, e = io.ReadAll(resp.Body)
		if e != nil {
			t.Fatal(e)
		}
		if body["stream"] == true && valid {
			if !strings.Contains(string(data), "[DONE]") {
				t.Fatal("stream missing done", string(data))
			}
			return string(data)
		}
		var result struct{ Code common.ErrorCode }
		if e := json.Unmarshal(data, &result); e != nil {
			t.Fatal(e)
		}
		if (result.Code == common.CodeSuccess) != valid {
			t.Fatal(path, result.Code, valid)
		}
		return string(data)
	}
	for _, p := range []string{"Mistral", "Jina", "Novita", "LongCat"} {
		for i := 0; i < 2; i++ {
			if p == "Mistral" || p == "Jina" {
				model := "mistral-embed"
				if p == "Jina" {
					model = "jina-embeddings-v3"
				}
				parsed, _ := url.Parse(api.URL)
				port, _ := strconv.Atoi(parsed.Port())
				httpClient := cli.NewHTTPClient()
				httpClient.Host = parsed.Hostname()
				httpClient.Port = port
				httpClient.APIKey = "hosted-owner"
				client := cli.NewMultiRAGClient("user")
				client.HTTPClient = httpClient
				command, e := cli.NewParser(fmt.Sprintf("embed text 'a' 'b' with '%s@%d@%s' dimension 2;", model, i, p)).Parse(false)
				if e != nil {
					t.Fatal(e)
				}
				result, e := client.ExecuteUserCommand(command)
				if e != nil {
					t.Fatal(e)
				}
				rows := result.(*cli.EmbeddingsResponse).Data
				if len(rows) != 2 || rows[1].Embedding[1] != float64(i+1) {
					t.Fatal(rows)
				}
				if p == "Jina" {
					request("/api/v1/rerank", "hosted-owner", map[string]any{"provider_name": p, "instance_name": fmt.Sprint(i), "model_name": "jina-reranker-v3", "query": "q", "documents": []string{"a", "b"}}, true)
				}
			} else {
				body := map[string]any{"provider_name": p, "instance_name": fmt.Sprint(i), "model_name": manager.FindProvider(p).Models[0].Name, "message": "q"}
				request("/api/v1/chat/completions", "hosted-owner", body, true)
				body["stream"] = true
				request("/api/v1/chat/completions", "hosted-owner", body, true)
			}
		}
	}
	snapshot := func() string {
		var value string
		e := readback.QueryRow(`SELECT (SELECT md5(string_agg(row_to_json(t)::text,',' ORDER BY id)) FROM tenant_model_instance t)||':'||(SELECT count(*)::text FROM tenant_model)||':'||(SELECT count(*)::text FROM tenant_model_provider)`).Scan(&value)
		if e != nil {
			t.Fatal(e)
		}
		return value
	}
	before, n := snapshot(), calls.Load()
	for _, id := range []string{"", "hosted-other", "hosted-owner"} {
		request("/api/v1/embeddings", id, map[string]any{"provider_name": "Mistral", "instance_name": "0", "model_name": "mistral-large-latest", "texts": []string{"q"}}, false)
	}
	request("/api/v1/chat/completions", "hosted-owner", map[string]any{"provider_name": "Jina", "instance_name": "0", "model_name": "jina-embeddings-v3", "message": "q"}, false)
	if snapshot() != before || calls.Load() != n {
		t.Fatal("HTTP denial changed SQL or contacted supplier")
	}
	t.Log("actual CLI embedding plus chat, SSE and rerank handlers; independent denial SQL snapshot and zero supplier calls passed")
}
