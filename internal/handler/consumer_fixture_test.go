package handler

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/testsupport"
	"multirag/internal/testsupport/scratch"
	"multirag/internal/utility"
)

type canonicalHTTPFixture struct {
	*scratch.Services
	ids, tokens, datasets map[string]string
}

func newCanonicalHTTPFixture(t *testing.T) *canonicalHTTPFixture {
	t.Helper()
	f := &canonicalHTTPFixture{Services: scratch.New(t), ids: map[string]string{}, tokens: map[string]string{}, datasets: map[string]string{}}
	previous := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = previous })
	if err := f.DB.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.Knowledgebase{}, &entity.Document{}, &entity.File{}, &entity.File2Document{}, &entity.APIToken{}, &entity.Connector{}, &entity.Connector2Kb{}, &entity.UserCanvas{}, &entity.PipelineOperationLog{}); err != nil {
		t.Fatal(err)
	}
	secret := f.Prefix + "consumer-auth"
	cfg := server.GetConfig()
	if cfg == nil {
		path := filepath.Join(t.TempDir(), "consumer-config.json")
		f.Journal.Event("reserve-consumer-config", map[string]any{"path": path})
		raw, _ := json.Marshal(map[string]any{"server": map[string]any{"secret_key": secret}})
		if err := os.WriteFile(path, raw, 0600); err != nil {
			t.Fatal(err)
		}
		if err := server.FromConfigFile(path); err != nil {
			t.Fatal(err)
		}
		cfg = server.GetConfig()
	}
	oldSecret := cfg.Server.SecretKey
	cfg.Server.SecretKey = &secret
	t.Cleanup(func() { cfg.Server.SecretKey = oldSecret })
	gate := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned canonical HTTP")
	t.Cleanup(func() { local.SetAdminStatus(gate.Status, gate.Reason) })
	active := "1"
	for _, name := range []string{"owner", "member", "invite", "revoked", "foreign", "disabled"} {
		id := f.Prefix + name
		f.ids[name] = id
		state := "1"
		if name == "disabled" {
			state = "0"
		}
		access := utility.GenerateToken()
		if err := f.DB.Create(&entity.User{ID: id, Email: id + "@fixture.invalid", Nickname: name, IsActive: state, Status: &state, AccessToken: &access}).Error; err != nil {
			t.Fatal(err)
		}
		token, err := utility.DumpAccessToken(access, secret)
		if err != nil {
			t.Fatal(err)
		}
		f.tokens[name] = token
	}
	for _, name := range []string{"owner", "foreign"} {
		if err := f.DB.Create(&entity.Tenant{ID: f.ids[name], Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, m := range []struct{ name, role, status string }{{"owner", "owner", "1"}, {"member", "normal", "1"}, {"invite", "invite", "1"}, {"revoked", "normal", "0"}, {"foreign", "owner", "1"}} {
		status := m.status
		tenant := f.ids["owner"]
		if m.name == "foreign" {
			tenant = f.ids["foreign"]
		}
		if err := f.DB.Create(&entity.UserTenant{ID: utility.GenerateToken(), UserID: f.ids[m.name], TenantID: tenant, Role: m.role, Status: &status}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, kind := range []string{"shared", "private", "foreign", "disabled", "empty"} {
		id := strings.ReplaceAll(uuid.Must(uuid.NewUUID()).String(), "-", "")
		f.datasets[kind] = id
		owner := f.ids["owner"]
		permission := "team"
		status := active
		if kind == "foreign" {
			owner = f.ids["foreign"]
		}
		if kind == "private" {
			permission = "me"
		}
		if kind == "disabled" {
			status = "0"
		}
		if err := f.DB.Create(&entity.Knowledgebase{ID: id, TenantID: owner, CreatedBy: owner, Name: kind, Permission: permission, Status: &status, ParserID: "naive", ParserConfig: entity.JSONMap{}}).Error; err != nil {
			t.Fatal(err)
		}
	}
	return f
}
func (f *canonicalHTTPFixture) app() *gin.Engine {
	app := gin.New()
	app.Use(gin.Recovery(), NewAuthHandler().AuthMiddleware())
	return app
}
func (f *canonicalHTTPFixture) serve(t *testing.T, app *gin.Engine) string {
	t.Helper()
	return testsupport.NewHTTP(t, app).URL
}
func (f *canonicalHTTPFixture) request(t *testing.T, url, actor, method, path string, body any) (int, map[string]any, []byte, http.Header) {
	t.Helper()
	var input io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		input = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(t.Context(), method, url+path, input)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	if actor != "" {
		req.Header.Set("Authorization", f.tokens[actor])
	}
	response, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	var result map[string]any
	_ = json.Unmarshal(raw, &result)
	return response.StatusCode, result, raw, response.Header
}
func (f *canonicalHTTPFixture) document(t *testing.T, kind, name string) string {
	t.Helper()
	id := utility.GenerateToken()
	status := "1"
	if err := f.DB.Create(&entity.Document{ID: id, KbID: f.datasets[kind], CreatedBy: f.ids["owner"], Name: &name, ParserID: "naive", ParserConfig: entity.JSONMap{}, Status: &status, Type: "txt", Suffix: "txt"}).Error; err != nil {
		t.Fatal(err)
	}
	file := utility.GenerateToken()
	if err := f.DB.Create(&entity.File{ID: file, ParentID: file, TenantID: f.ids["owner"], CreatedBy: f.ids["owner"], Name: name, Type: "file"}).Error; err != nil {
		t.Fatal(err)
	}
	if err := f.DB.Create(&entity.File2Document{ID: utility.GenerateToken(), FileID: &file, DocumentID: &id}).Error; err != nil {
		t.Fatal(err)
	}

	return id
}
