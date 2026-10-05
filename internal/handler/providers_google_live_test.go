package handler_test

import (
	"bytes"
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/handler"
	"multirag/internal/server"
	"multirag/internal/service"

	"github.com/gin-gonic/gin"
)

// This opt-in acceptance test uses a private database, real TCP listeners and
// the real Google SDK. Authentication is a fixture bearer token, not a production identity provider.
func TestGoogleProviderLiveHTTPAndSQL(t *testing.T) {
	configPath := os.Getenv("MULTIRAG_GOOGLE_LIVE_CONFIG")
	if configPath == "" {
		t.Skip("requires owned scratch database config")
	}
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(configPath); err != nil {
		t.Fatal("read owned config failed")
	}
	if !strings.HasPrefix(server.GetConfig().Database.Database, "multirag_google_") {
		t.Fatal("live test requires an owned Google scratch DB")
	}
	if err := dao.InitDB(); err != nil {
		t.Fatal("initialize scratch DB failed")
	}
	pool, err := dao.DB.DB()
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	dsn, err := dao.PostgresDSN(server.GetConfig().Database)
	if err != nil {
		t.Fatal(err)
	}
	for _, table := range []string{"tenant_model", "tenant_model_instance", "tenant_model_provider", "t_ai_user_tenants"} {
		if err := dao.DB.Exec("DELETE FROM " + table).Error; err != nil {
			t.Fatal("reset owned fixture rows failed")
		}
	}
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("open independent SQL readback failed")
	}
	defer readback.Close()
	var listRequests, chatRequests atomic.Int32
	streamCancelled := make(chan struct{}, 1)
	streamStarted := make(chan struct{}, 1)
	providerServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("x-goog-api-key") != "controlled-google-fixture" || r.URL.Query().Has("key") {
			t.Error("SDK credential location mismatch")
		}
		if r.Method == "GET" {
			listRequests.Add(1)
			if r.URL.Query().Get("pageToken") == "" {
				fmt.Fprint(w, `{"models":[{"name":"models/page-one"}],"nextPageToken":"second"}`)
			} else {
				fmt.Fprint(w, `{"models":[{"name":"models/page-two"}]}`)
			}
			return
		}
		chatRequests.Add(1)
		var body struct {
			Contents         []struct{ Parts []struct{ Text string } }
			GenerationConfig struct {
				ThinkingConfig struct{ IncludeThoughts bool }
			}
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		if len(body.Contents) != 1 || len(body.Contents[0].Parts) != 1 {
			t.Error("invalid text request")
			return
		}
		message := body.Contents[0].Parts[0].Text
		streaming := strings.HasSuffix(r.URL.Path, ":streamGenerateContent")
		if message == "provider-error" {
			w.WriteHeader(401)
			fmt.Fprint(w, `{"error":{"code":401,"message":"controlled-google-fixture private provider SQL /secret/path"}}`)
			return
		}
		if message == "cancel" {
			w.Header().Set("Content-Type", "text/event-stream")
			w.WriteHeader(200)
			w.(http.Flusher).Flush()
			streamStarted <- struct{}{}
			<-r.Context().Done()
			streamCancelled <- struct{}{}
			return
		}
		if message == "empty-candidates" {
			if streaming {
				fmt.Fprint(w, "data: {}\n\n")
			} else {
				fmt.Fprint(w, `{}`)
			}
			return
		}
		if message == "empty-parts" {
			if streaming {
				fmt.Fprint(w, "data: {\"candidates\":[{\"content\":{\"parts\":[]}}]}\n\n")
			} else {
				fmt.Fprint(w, `{"candidates":[{"content":{"parts":[]}}]}`)
			}
			return
		}
		if message == "default-thinking" && !body.GenerationConfig.ThinkingConfig.IncludeThoughts {
			t.Error("default thinking not sent")
		}
		if message == "no-thinking" && body.GenerationConfig.ThinkingConfig.IncludeThoughts {
			t.Error("false thinking overwritten")
		}
		response := `{"candidates":[{"content":{"parts":[{"text":"reason","thought":true},{"text":"answer"}]}}]}`
		if streaming {
			w.Header().Set("Content-Type", "text/event-stream")
			fmt.Fprintf(w, "data: {}\n\ndata: %s\n\n", response)
		} else {
			fmt.Fprint(w, response)
		}
	}))
	defer func() {
		providerServer.Close()
		if conn, err := net.DialTimeout("tcp", strings.TrimPrefix(providerServer.URL, "http://"), 100*time.Millisecond); err == nil {
			conn.Close()
			t.Error("owned provider listener still open")
		}
	}()
	manager := dao.GetModelProviderManager()
	google := manager.FindProvider("Google")
	if google == nil {
		t.Fatal("Google config not loaded")
	}
	originalGoogleURL := google.URL["default"]
	if originalGoogleURL != "https://generativelanguage.googleapis.com" {
		t.Fatal("Google default URL changed")
	}
	google.URL["fixture"] = providerServer.URL
	driver, ok := google.ModelDriver.(*models.GoogleModel)
	if !ok {
		t.Fatal("factory not Google SDK driver")
	}
	driver.BaseURL["fixture"] = providerServer.URL
	deepseek := manager.FindProvider("DeepSeek")
	originalDeepseekURL := deepseek.URL["default"]
	active := "1"
	if err := dao.DB.Create(&entity.UserTenant{ID: "google-relation", UserID: "google-user", TenantID: "google-tenant", Role: "owner", InvitedBy: "google-user", Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	svc := service.NewModelProviderService()
	h := handler.NewProviderHandler(nil, svc)
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	random := make([]byte, 24)
	if _, err := rand.Read(random); err != nil {
		t.Fatal(err)
	}
	token := hex.EncodeToString(random)
	router.Use(func(c *gin.Context) {
		if c.GetHeader("Authorization") != "Bearer "+token {
			c.AbortWithStatus(401)
			return
		}
		c.Set("user_id", "google-user")
		c.Set("user", &entity.User{ID: "google-user"})
		c.Next()
	})
	base := "/api/v1/providers"
	router.PUT(base+"/", h.AddProvider)
	router.POST(base+"/:provider_name/instances", h.CreateProviderInstance)
	router.GET(base+"/:provider_name/instances/:instance_name/connection", h.CheckProviderConnection)
	router.GET(base+"/:provider_name/instances/:instance_name/models", h.ListInstanceModels)
	router.PATCH(base+"/:provider_name/instances/:instance_name/models/*model_name", h.EnableOrDisableModel)
	router.POST("/api/v1/chat/completions", h.ChatToModel)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	httpServer := &http.Server{Handler: router, ReadHeaderTimeout: 5 * time.Second}
	serveDone := make(chan error, 1)
	go func() { serveDone <- httpServer.Serve(listener) }()
	defer func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := httpServer.Shutdown(ctx); err != nil {
			t.Error(err)
		}
		if err := <-serveDone; !errors.Is(err, http.ErrServerClosed) {
			t.Error(err)
		}
		if conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond); err == nil {
			conn.Close()
			t.Error("owned listener still open")
		}
	}()
	client := &http.Client{Timeout: 10 * time.Second}
	request := func(method, path string, body any) (int, string) {
		t.Helper()
		data, _ := json.Marshal(body)
		req, err := http.NewRequest(method, "http://"+address+path, bytes.NewReader(data))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Authorization", "Bearer "+token)
		req.Header.Set("Content-Type", "application/json")
		response, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		data, err = io.ReadAll(response.Body)
		if err != nil {
			t.Fatal(err)
		}
		return response.StatusCode, string(data)
	}
	assertSuccess := func(body string) {
		t.Helper()
		var result struct{ Code int }
		if err := json.Unmarshal([]byte(body), &result); err != nil || result.Code != 0 {
			t.Fatalf("HTTP business failure %s", body)
		}
	}
	_, body := request("PUT", base+"/", map[string]any{"provider_name": "Google"})
	assertSuccess(body)
	_, body = request("PUT", base+"/", map[string]any{"provider_name": "DeepSeek"})
	assertSuccess(body)
	_, body = request("POST", base+"/Google/instances", map[string]any{"instance_name": "controlled", "api_key": "controlled-google-fixture", "region": "fixture"})
	assertSuccess(body)
	path := base + "/Google/instances/controlled"
	_, body = request("GET", path+"/connection", nil)
	assertSuccess(body)
	_, body = request("GET", path+"/models?supported=true", nil)
	assertSuccess(body)
	if !strings.Contains(body, "models/page-one") || !strings.Contains(body, "models/page-two") || listRequests.Load() != 4 {
		t.Fatal("HTTP list did not read both pages")
	}
	_, body = request("GET", path+"/models", nil)
	assertSuccess(body)
	if !strings.Contains(body, "gemini-2.5-flash") {
		t.Fatal("configured model missing")
	}
	chat := func(message string, stream bool, thinking *bool) (int, string) {
		b := map[string]any{"provider_name": "Google", "instance_name": "controlled", "model_name": "gemini-2.5-flash", "message": message, "stream": stream}
		if thinking != nil {
			b["thinking"] = *thinking
		}
		return request("POST", "/api/v1/chat/completions", b)
	}
	_, body = chat("default-thinking", false, nil)
	assertSuccess(body)
	if !strings.Contains(body, `"answer":"answer"`) || !strings.Contains(body, `"reasoning_content":"reason"`) {
		t.Fatalf("answer %s", body)
	}
	off := false
	_, body = chat("no-thinking", false, &off)
	assertSuccess(body)
	if strings.Contains(body, `"reasoning_content":"reason"`) {
		t.Fatal("explicit false leaked thought")
	}
	_, body = chat("default-thinking", true, nil)
	if strings.Index(body, "[REASONING]reason") < 0 || strings.Index(body, "[MESSAGE]answer") < strings.Index(body, "[REASONING]reason") || !strings.Contains(body, "event:done") {
		t.Fatalf("SSE order %s", body)
	}
	for _, message := range []string{"provider-error", "empty-candidates", "empty-parts"} {
		for _, stream := range []bool{false, true} {
			_, body = chat(message, stream, nil)
			if strings.Contains(body, "controlled-google-fixture") || strings.Contains(body, "private provider") || strings.Contains(body, "/secret") {
				t.Fatal("unsafe HTTP error")
			}
			if stream {
				if !strings.Contains(body, "event:error") || strings.Contains(body, "event:done") {
					t.Fatal("failed SSE claimed success")
				}
			} else {
				var result struct{ Code int }
				json.Unmarshal([]byte(body), &result)
				if result.Code == 0 {
					t.Fatal("failed provider returned business success")
				}
			}
		}
	}
	callbackFailure := errors.New("callback failure")
	code, err := svc.ChatToModelStreamWithSender("Google", "controlled", "gemini-2.5-flash", "google-user", "default-thinking", &models.APIConfig{}, nil, func(*string, *string) error { return callbackFailure })
	if code == common.CodeSuccess || !errors.Is(err, callbackFailure) {
		t.Fatalf("service lost callback error: %v %v", code, err)
	}
	// A configured slash-valued model must persist under its complete name.
	google.Models = append(google.Models, &entity.Model{Name: "models/slash-fixture", ModelTypes: []string{"chat"}})
	_, body = request("PATCH", path+"/models/models/slash-fixture", map[string]any{"status": "disabled"})
	assertSuccess(body)
	var slashCount int
	if err := readback.QueryRow(`SELECT count(*) FROM tenant_model WHERE model_name='models/slash-fixture' AND status='disabled'`).Scan(&slashCount); err != nil || slashCount != 1 {
		t.Fatal("slash model independent readback failed")
	}
	_, body = request("PATCH", path+"/models/models/slash-fixture", map[string]any{"status": "enabled"})
	assertSuccess(body)
	_, body = request("PATCH", path+"/models/gemini-2.5-flash", map[string]any{"status": "disabled"})
	assertSuccess(body)
	var count int
	if err := readback.QueryRow(`SELECT count(*) FROM tenant_model WHERE model_name='gemini-2.5-flash' AND status='disabled'`).Scan(&count); err != nil || count != 1 {
		t.Fatal("independent disabled model readback failed")
	}
	before := chatRequests.Load()
	_, body = chat("default-thinking", true, nil)
	if !strings.Contains(body, "event:error") || chatRequests.Load() != before {
		t.Fatal("disabled model reached provider")
	}
	_, body = request("PATCH", path+"/models/gemini-2.5-flash", map[string]any{"status": "enabled"})
	assertSuccess(body)
	if err := readback.QueryRow(`SELECT count(*) FROM tenant_model`).Scan(&count); err != nil || count != 0 {
		t.Fatal("independent enabled model readback failed")
	}
	var region string
	if err := readback.QueryRow(`SELECT extra::json->>'region' FROM tenant_model_instance WHERE instance_name='controlled'`).Scan(&region); err != nil || region != "fixture" {
		t.Fatal("independent instance config readback failed")
	}
	var names []string
	rows, err := readback.Query(`SELECT provider_name FROM tenant_model_provider ORDER BY provider_name`)
	if err != nil {
		t.Fatal(err)
	}
	for rows.Next() {
		var n string
		rows.Scan(&n)
		names = append(names, n)
	}
	rows.Close()
	if !reflect.DeepEqual(names, []string{"DeepSeek", "Google"}) {
		t.Fatal("other provider not preserved")
	}
	if google.URL["default"] != originalGoogleURL || deepseek.URL["default"] != originalDeepseekURL {
		t.Fatal("other provider config mutated")
	}
	ctx, cancel := context.WithCancel(context.Background())
	req, _ := http.NewRequestWithContext(ctx, "POST", "http://"+address+"/api/v1/chat/completions", strings.NewReader(`{"provider_name": "Google", "instance_name": "controlled", "model_name":"gemini-2.5-flash","message":"cancel","stream":true}`))
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	response, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	select {
	case <-streamStarted:
	case <-time.After(5 * time.Second):
		cancel()
		t.Fatal("provider stream did not start")
	}
	cancel()
	response.Body.Close()
	select {
	case <-streamCancelled:
	case <-time.After(5 * time.Second):
		t.Fatal("HTTP cancellation did not cancel provider SDK request")
	}
	before = chatRequests.Load()
	if err := dao.DB.Exec("ALTER TABLE tenant_model RENAME TO google_fixture_unavailable_models").Error; err != nil {
		t.Fatal(err)
	}
	code, dbErr := svc.ChatToModelStreamWithSender("Google", "controlled", "gemini-2.5-flash", "google-user", "default-thinking", nil, nil, func(*string, *string) error { return nil })
	restoreErr := dao.DB.Exec("ALTER TABLE google_fixture_unavailable_models RENAME TO tenant_model").Error
	if restoreErr != nil {
		t.Fatal(restoreErr)
	}
	if code == common.CodeSuccess || dbErr == nil || chatRequests.Load() != before {
		t.Fatal("SQL failure was treated as a missing disabled-model row")
	}
	code, missingErr := svc.ChatToModelStreamWithSender("Google", "controlled", "gemini-2.5-flash", "missing-user", "hello", nil, nil, func(*string, *string) error { return nil })
	if code == common.CodeSuccess || missingErr == nil {
		t.Fatal("missing tenant lost its service error")
	}
	t.Log("real listener, fixture bearer, handler/service/DAO, Google SDK requests, safe JSON/SSE failures, model/region/provider SQL readback and client cancellation passed; owned listeners shut down")
}
