package admin

import (
	"encoding/json"
	"github.com/gin-gonic/gin"
	"io"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/testsupport"
	"multirag/internal/testsupport/scratch"
	"multirag/internal/utility"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type adminFixture struct {
	*scratch.Services
	service *Service
	url     string
	tokens  map[string]string
	client  *http.Client
}

func newAdminFixture(t *testing.T) *adminFixture {
	t.Helper()
	f := scratch.New(t)
	old := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = old })
	if err := common.Init("error", ""); err != nil {
		t.Fatal(err)
	}
	config := filepath.Join(t.TempDir(), "admin.yaml")
	f.Journal.Event("reserve-local-config", map[string]interface{}{"path": config})
	if err := os.WriteFile(config, []byte("server:\n  mode: test\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal(err)
	}
	secret := "owned-admin-signing-secret-32-characters"
	server.GetConfig().Server.SecretKey = &secret
	for _, m := range []any{&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.File{}, &entity.Document{}, &entity.Task{}, &entity.Knowledgebase{}, &entity.UserCanvas{}, &entity.UserCanvasVersion{}, &entity.Chat{}, &entity.ChatSession{}, &entity.APIToken{}, &entity.API4Conversation{}, &entity.TenantLLM{}, &entity.TenantLangfuse{}, &entity.TenantModelProvider{}, &entity.TenantModelInstance{}, &entity.TenantModel{}, &entity.TenantModelGroup{}, &entity.TenantModelGroupMapping{}, &entity.LLM{}, &entity.SystemSettings{}, &entity.IngestionTask{}, &entity.IngestionTaskLog{}, &entity.IngestionTasklet{}, &entity.IngestionTaskletLog{}} {
		if err := f.DB.AutoMigrate(m); err != nil {
			t.Fatal(err)
		}
	}
	fixture := &adminFixture{Services: f, service: NewService(), tokens: map[string]string{}}
	for _, who := range []string{"admin", "normal", "inactive", "nil-admin"} {
		access := utility.GenerateToken()
		status := "1"
		yes := who == "admin" || who == "inactive"
		var super *bool = &yes
		if who == "nil-admin" {
			super = nil
		}
		active := "1"
		if who == "inactive" {
			active = "0"
		}
		if err := f.DB.Create(&entity.User{ID: who, Email: who + "@fixture.invalid", Nickname: who, AccessToken: &access, Status: &status, IsActive: active, IsSuperuser: super}).Error; err != nil {
			t.Fatal(err)
		}
		token, err := utility.DumpAccessToken(access, secret)
		if err != nil {
			t.Fatal(err)
		}
		fixture.tokens[who] = token
	}
	gin.SetMode(gin.TestMode)
	app := gin.New()
	app.Use(gin.Recovery())
	NewRouter(NewHandler(fixture.service)).Setup(app)
	srv := testsupport.NewHTTP(t, app)
	fixture.url = srv.URL
	fixture.client = srv.Client()
	return fixture
}
func (f *adminFixture) request(t *testing.T, actor, method, path, body string) (int, map[string]any) {
	t.Helper()
	req, err := http.NewRequest(method, f.url+"/api/v1/admin"+path, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	if actor != "" {
		req.Header.Set("Authorization", "Bearer "+f.tokens[actor])
	}
	res, err := f.client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	raw, err := io.ReadAll(res.Body)
	if err != nil {
		t.Fatal(err)
	}
	var result map[string]any
	if err := json.Unmarshal(raw, &result); err != nil {
		t.Fatalf("invalid HTTP %d: %s", res.StatusCode, raw)
	}
	return res.StatusCode, result
}
