package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/service/nlp"
	"multirag/internal/testsupport"
	"multirag/internal/testsupport/scratch"
	"multirag/internal/utility"
)

// Exercise the frozen original AuthMiddleware, six-argument Dify handler and
// actual SQL accessibility. Metadata/model/retrieval are controlled boundaries.
func TestDifyCredentialSQLAndScope(t *testing.T) {
	f := scratch.New(t)
	previous := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = previous })
	if err := f.DB.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.Knowledgebase{}, &entity.APIToken{}); err != nil {
		t.Fatal(err)
	}
	secret := f.Prefix + "dify-auth"
	cfg := server.GetConfig()
	if cfg == nil {
		path := filepath.Join(t.TempDir(), "dify-service.json")
		f.Journal.Event("reserve-dify-config", map[string]any{"path": path})
		raw, _ := json.Marshal(map[string]any{"server": map[string]any{"secret_key": secret}})
		if err := os.WriteFile(path, raw, 0600); err != nil {
			t.Fatal(err)
		}
		if err := server.FromConfigFile(path); err != nil {
			t.Fatal(err)
		}
		cfg = server.GetConfig()
	}
	priorSecret := cfg.Server.SecretKey
	cfg.Server.SecretKey = &secret
	t.Cleanup(func() { cfg.Server.SecretKey = priorSecret })
	license := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned Dify fixture")
	t.Cleanup(func() { local.SetAdminStatus(license.Status, license.Reason) })
	active := "1"
	ids := map[string]string{}
	tokens := map[string]string{}
	for _, name := range []string{"owner", "member", "invite", "revoked", "foreign", "disabled"} {
		id := f.Prefix + name
		ids[name] = id
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
		tokens[name] = token
	}
	if err := f.DB.Create(&entity.Tenant{ID: ids["owner"], Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	for _, m := range []struct{ name, role, status string }{{"owner", "owner", "1"}, {"member", "normal", "1"}, {"invite", "invite", "1"}, {"revoked", "normal", "0"}} {
		status := m.status
		if err := f.DB.Create(&entity.UserTenant{ID: utility.GenerateToken(), UserID: ids[m.name], TenantID: ids["owner"], Role: m.role, Status: &status}).Error; err != nil {
			t.Fatal(err)
		}
	}
	kb := f.Prefix + "kb"
	if err := f.DB.Create(&entity.Knowledgebase{ID: kb, TenantID: ids["owner"], CreatedBy: ids["owner"], Name: kb, EmbdID: "selected", Permission: "team", Status: &active, ParserConfig: entity.JSONMap{}}).Error; err != nil {
		t.Fatal(err)
	}
	modelCalls, retrievalCalls := 0, 0
	model := &mockModelService{getEmbeddingFn: func(tenant, model string) (*models.EmbeddingModel, error) {
		modelCalls++
		if tenant != ids["owner"] || model != "selected" {
			return nil, fmt.Errorf("lost selected binding")
		}
		return &models.EmbeddingModel{}, nil
	}}
	retrieval := &mockRetrievalService{retrievalFn: func(_ context.Context, req *nlp.RetrievalRequest) (*nlp.RetrievalResult, error) {
		retrievalCalls++
		if len(req.DocIDs) != 1 || req.DocIDs[0] != service.NoMatchDocIDSentinel || len(req.KbIDs) != 1 || req.KbIDs[0] != kb || len(req.TenantIDs) != 1 || req.TenantIDs[0] != ids["owner"] {
			return nil, fmt.Errorf("metadata no-match broadened or scope lost: %+v", req)
		}
		return &nlp.RetrievalResult{}, nil
	}}
	h := NewDifyRetrievalHandler(dao.NewKnowledgebaseDAO(), model, &mockMetadataService{}, retrieval, &mockDocDAO{}, &mockDocEngine{})
	app := gin.New()
	app.Use(NewAuthHandler().AuthMiddleware())
	app.POST("/retrieval", h.Retrieval)
	api := testsupport.NewHTTP(t, app)
	body := []byte(fmt.Sprintf(`{"knowledge_id":%q,"query":"q","metadata_condition":{"conditions":[{"name":"category","comparison_operator":"is","value":"missing"}]}}`, kb))
	for _, actor := range []string{"", "owner", "member", "invite", "revoked", "foreign", "disabled", "invalid"} {
		beforeModel, beforeRetrieval := modelCalls, retrievalCalls
		req, err := http.NewRequestWithContext(t.Context(), http.MethodPost, api.URL+"/retrieval", bytes.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		token := tokens[actor]
		if actor == "invalid" {
			token = "Bearer invalid"
		}
		if token != "" {
			req.Header.Set("Authorization", token)
		}
		req.Header.Set("Content-Type", "application/json")
		response, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		raw, err := io.ReadAll(response.Body)
		response.Body.Close()
		if err != nil {
			t.Fatal(err)
		}
		if actor == "owner" || actor == "member" {
			if response.StatusCode != 200 || modelCalls != beforeModel+1 || retrievalCalls != beforeRetrieval+1 {
				t.Fatal("authorized Dify", actor, response.StatusCode, string(raw))
			}
		} else if response.StatusCode != http.StatusUnauthorized || modelCalls != beforeModel || retrievalCalls != beforeRetrieval {
			t.Fatal("denied Dify touched provider/retrieval", actor, response.StatusCode, string(raw))
		}
	}
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM knowledgebase WHERE id=? AND tenant_id=?", kb, ids["owner"]).Scan(&count); err != nil || count != 1 {
		t.Fatal("independent SQL scope readback", count, err)
	}
	f.Journal.Event("canonical-dify-scope-readback", map[string]any{"original_auth": true, "foreign_invite_revoked_disabled_zero_provider_retrieval": true, "empty_metadata_keeps_no_match_sentinel": true, "controlled_model_retrieval": true})
}
