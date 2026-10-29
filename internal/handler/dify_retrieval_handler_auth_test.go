package handler

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/testsupport"
	"multirag/internal/utility"
	"multirag/tests/support"
)

func TestDifyRejectsMalformedCredentialHeaders(t *testing.T) {
	for _, headers := range [][]string{nil, {""}, {"token"}, {"Bearer "}, {"Bearer  token"}, {"Bearer token "}, {"Bearer a,b"}, {"Bearer a", "Bearer b"}, {"Basic token"}} {
		if _, _, err := resolveDifyCredential(context.Background(), headers, "owned-test-key"); err == nil {
			t.Fatal("invalid header accepted")
		}
	}
}

type difyAuthSigningStore struct{}

func (difyAuthSigningStore) Get(string) (string, error) {
	return "owned-dify-signing-key-32-bytes-minimum", nil
}
func (difyAuthSigningStore) Set(string, string, time.Duration) bool   { return true }
func (difyAuthSigningStore) SetNX(string, string, time.Duration) bool { return true }

// This test uses real JWT/API middleware and a real TCP SQL schema. Metadata
// returns an authoritative empty match, making every denial's forbidden model,
// children and retrieval IO observable without a provider dependency.
func TestDifyCredentialSQLAndScope(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_DIFY_POSTGRES_DSN")
	if dsn == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned MULTIRAG_DIFY_POSTGRES_DSN required")
		}
		t.Skip("requires an owned Dify scratch database")
	}
	parsed, err := url.Parse(dsn)
	if err != nil || parsed.Hostname() == "" || parsed.Port() == "" || !strings.HasPrefix(strings.TrimPrefix(parsed.Path, "/"), "multirag_afteraab_") {
		t.Fatal("owned afteraab database at an explicit TCP endpoint required")
	}
	journal := testsupport.NewCase(t)
	t.Setenv("DISABLE_SDK", "")
	schema := "dify_auth_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	journal.Event("reserve-sql-schema", map[string]any{"endpoint": parsed.Host, "database": strings.TrimPrefix(parsed.Path, "/"), "schema": schema, "tables": []string{"t_ai_users", "t_ai_tenants", "t_ai_user_tenants", "t_ai_api_tokens", "t_ai_knowledgebases"}})
	admin, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("open owned SQL endpoint")
	}
	t.Cleanup(func() { admin.Close() })
	var absent bool
	if err := admin.QueryRowContext(t.Context(), "SELECT NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname=$1)", schema).Scan(&absent); err != nil || !absent {
		t.Fatal("schema precondition failed")
	}
	var pool, reader *sql.DB
	t.Cleanup(func() {
		if pool != nil {
			pool.Close()
		}
		if reader != nil {
			reader.Close()
		}
		if _, err := admin.ExecContext(context.Background(), "DROP SCHEMA IF EXISTS "+schema+" CASCADE"); err != nil {
			t.Error("drop owned schema failed")
			return
		}
		var exists bool
		if err := admin.QueryRow("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname=$1)", schema).Scan(&exists); err != nil || exists {
			t.Error("schema cleanup readback failed")
			return
		}
		journal.Event("sql-schema-cleaned", map[string]any{"schema": schema, "independent_readback": true})
	})
	if _, err := admin.ExecContext(t.Context(), "CREATE SCHEMA "+schema); err != nil {
		t.Fatal("create owned schema")
	}
	query := parsed.Query()
	query.Set("search_path", schema)
	parsed.RawQuery = query.Encode()
	db, err := gorm.Open(postgres.Open(parsed.String()), &gorm.Config{Logger: logger.Discard})
	if err != nil {
		t.Fatal("connect owned schema")
	}
	pool, err = db.DB()
	if err != nil {
		t.Fatal(err)
	}
	reader, err = sql.Open("pgx", parsed.String())
	if err != nil {
		t.Fatal(err)
	}
	previousDB := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = previousDB })
	if err := db.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.APIToken{}, &entity.Knowledgebase{}); err != nil {
		t.Fatal("migrate auth fixtures")
	}
	active, inactive := "1", "0"
	for _, id := range []string{"owner", "other", "disabled"} {
		user := entity.User{ID: id, Email: id + "@dify.invalid", Nickname: id, Status: &active}
		if err := db.Create(&user).Error; err != nil {
			t.Fatal("create fixture user")
		}
	}
	if err := db.Model(&entity.User{}).Where("id=?", "disabled").Update("is_active", false).Error; err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"owner", "foreign", "alias", "off", "offshared", "offmember", "invaliduser"} {
		status := &active
		if id == "off" || id == "offshared" {
			status = &inactive
		}
		if err := db.Create(&entity.Tenant{ID: id, Status: status}).Error; err != nil {
			t.Fatal("create fixture tenant")
		}
		user := "owner"
		if id == "foreign" || id == "offshared" {
			user = "other"
		}
		if id == "invaliduser" {
			user = "disabled"
		}
		membershipStatus := &active
		if id == "offmember" {
			membershipStatus = &inactive
		}
		if err := db.Create(&entity.UserTenant{ID: id, UserID: user, TenantID: id, Role: "owner", Status: membershipStatus}).Error; err != nil {
			t.Fatal("create owner membership")
		}
		kbStatus := status
		if id == "offshared" {
			kbStatus = &active
		}
		if err := db.Create(&entity.Knowledgebase{ID: id + "-kb", TenantID: id, Name: id, CreatedBy: user, Permission: "team", Status: kbStatus, ParserConfig: entity.JSONMap{}}).Error; err != nil {
			t.Fatal("create fixture dataset")
		}
		if err := db.Create(&entity.APIToken{TenantID: id, Token: "key-" + id}).Error; err != nil {
			t.Fatal("create fixture API key")
		}
	}
	if err := db.Create(&entity.UserTenant{ID: "shared", UserID: "owner", TenantID: "foreign", Role: "normal", Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Create(&entity.UserTenant{ID: "offshared-member", UserID: "owner", TenantID: "offshared", Role: "normal", Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	beta := "public-beta-key"
	for _, token := range []entity.APIToken{{TenantID: "owner", Token: "duplicate"}, {TenantID: "foreign", Token: "duplicate"}, {TenantID: "owner", Token: "ordinary-key", Beta: &beta}} {
		if err := db.Create(&token).Error; err != nil {
			t.Fatal(err)
		}
	}
	snapshot := func() string {
		t.Helper()
		var result strings.Builder
		for _, table := range []string{"t_ai_users", "t_ai_tenants", "t_ai_user_tenants", "t_ai_api_tokens", "t_ai_knowledgebases"} {
			var value string
			if err := reader.QueryRow("SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text,'[]') FROM " + table + " t").Scan(&value); err != nil {
				t.Fatal("independent SQL snapshot")
			}
			result.WriteString(value)
		}
		return fmt.Sprintf("%x", sha256.Sum256([]byte(result.String())))
	}
	before := snapshot()
	if err := server.InitVariables(difyAuthSigningStore{}); err != nil {
		t.Fatal(err)
	}
	priorLicense := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned Dify acceptance")
	t.Cleanup(func() { local.SetAdminStatus(priorLicense.Status, priorLicense.Reason) })
	jwtToken, err := utility.SignUserJWT("owner@dify.invalid", server.GetSecretKey())
	if err != nil {
		t.Fatal(err)
	}
	wrongJWT, err := utility.SignUserJWT("owner@dify.invalid", "wrong-owned-signing-key")
	if err != nil {
		t.Fatal(err)
	}
	d := &difyTestDependencies{metadata: common.MetaData{}}
	h := NewDifyRetrievalHandler(DifyRetrievalDependencies{Datasets: dao.NewKnowledgebaseDAO(), Metadata: d})
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	app.POST("/retrieval", NewAuthHandler().DifyAuthMiddleware(), h.Retrieval)
	api := support.RegisteredHTTPServer(t, app)
	client := &http.Client{Timeout: 5 * time.Second}
	for _, test := range []struct {
		name, token, kb string
		status          int
	}{
		{"owner API", "key-owner", "owner-kb", 200},
		{"alias original tenant", "key-alias", "alias-kb", 200},
		{"JWT shared KB", jwtToken, "foreign-kb", 200},
		{"JWT inactive foreign tenant active KB", jwtToken, "offshared-kb", 403},
		{"API shared KB denied", "key-owner", "foreign-kb", 403},
		{"same owner foreign token tenant", "key-owner", "alias-kb", 403},
		{"duplicate token", "duplicate", "owner-kb", 403},
		{"beta key", beta, "owner-kb", 403},
		{"inactive tenant", "key-off", "off-kb", 403},
		{"inactive membership", "key-offmember", "offmember-kb", 403},
		{"inactive user", "key-invaliduser", "invaliduser-kb", 403},
		{"wrong JWT", wrongJWT, "owner-kb", 403},
	} {
		t.Run(test.name, func(t *testing.T) {
			d.calls = nil
			request, err := http.NewRequestWithContext(t.Context(), http.MethodPost, api.URL+"/retrieval", strings.NewReader(`{"knowledge_id":"`+test.kb+`","query":"question","use_kg":true,"metadata_condition":{"conditions":[{"name":"x","comparison_operator":"=","value":1}]}}`))
			if err != nil {
				t.Fatal(err)
			}
			request.Header.Set("Authorization", "Bearer "+test.token)
			response, err := client.Do(request)
			if err != nil {
				t.Fatal(err)
			}
			body, err := io.ReadAll(response.Body)
			response.Body.Close()
			if err != nil || response.StatusCode != test.status {
				t.Fatal(response.StatusCode, string(body), err)
			}
			if test.status != 200 && len(d.calls) != 0 {
				t.Fatal("denial touched downstream resources", d.calls)
			}
			if test.status == 200 && (len(d.calls) != 1 || d.calls[0] != "filter" || string(body) != `{"records":[]}`) {
				t.Fatal("no-match touched forbidden resources", string(body), d.calls)
			}
		})
	}
	if after := snapshot(); after != before {
		t.Fatal("HTTP authentication/retrieval changed SQL state")
	}
	journal.Event("dify-auth-readback", map[string]any{"endpoint": parsed.Host, "schema": schema, "snapshot_sha256": before, "original_token_tenant_preserved": true, "shared_JWT_allowed": true, "denial_downstream_calls": 0, "sql_unchanged": true})
}
