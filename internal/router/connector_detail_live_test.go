package router

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
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
	"multirag/internal/handler"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/utility"
)

type connectorOutboundTrap struct{ calls atomic.Int64 }

func (trap *connectorOutboundTrap) RoundTrip(*http.Request) (*http.Response, error) {
	trap.calls.Add(1)
	return nil, errors.New("connector detail must not call a source")
}

func TestConnectorDetailHTTPJWTAndSQL(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_CONNECTOR_DETAIL_DSN")
	if dsn == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned connector detail scratch database required")
		}
		t.Skip("requires owned connector detail scratch database")
	}
	parsed, err := url.Parse(dsn)
	if err != nil || !strings.HasPrefix(strings.TrimPrefix(parsed.Path, "/"), "multirag_after0fb_g_connector_") {
		t.Fatal("unowned scratch database")
	}
	reportDir := os.Getenv("MULTIRAG_CONNECTOR_DETAIL_REPORT_DIR")
	address := os.Getenv("MULTIRAG_CONNECTOR_DETAIL_ADDRESS")
	if reportDir == "" || address == "" {
		t.Fatal("pre-registered report directory and listener required")
	}
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	setup, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal(err)
	}
	setupPool, err := setup.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { setupPool.Close() })
	ids := []string{"owner", "member", "admin", "joined-owner", "foreign", "disabled", "invited", "inactive-user", "superuser"}
	cases := []struct {
		name, actor, id  string
		status           int
		code             common.ErrorCode
		license, dbError bool
	}{
		{"missing-auth", "", "owner-connector", 401, 401, false, false},
		{"invalid-jwt", "invalid", "owner-connector", 401, common.CodeUnauthorized, false, false},
		{"owner-without-membership", "owner", "owner-connector", 200, 0, false, false},
		{"member-distinct-tenant", "member", "owner-connector", 200, 0, false, false},
		{"admin", "admin", "owner-connector", 200, 0, false, false},
		{"joined-owner-distinct-tenant", "joined-owner", "owner-connector", 200, 0, false, false},
		{"foreign-tenant", "foreign", "owner-connector", 200, common.CodeAuthenticationError, false, false},
		{"unknown-id", "member", "unknown-connector", 200, common.CodeAuthenticationError, false, false},
		{"disabled-membership", "disabled", "owner-connector", 200, common.CodeAuthenticationError, false, false},
		{"invited-membership", "invited", "owner-connector", 200, common.CodeAuthenticationError, false, false},
		{"member-cannot-access-foreign", "member", "foreign-connector", 200, common.CodeAuthenticationError, false, false},
		{"inactive-user", "inactive-user", "owner-connector", 401, common.CodeUnauthorized, false, false},
		{"superuser-existing-boundary", "superuser", "owner-connector", 403, common.CodeForbidden, false, false},
		{"license-existing-boundary", "owner", "owner-connector", 503, common.CodeUnauthorized, true, false},
		{"database-error-sanitized", "owner", "owner-connector", 200, common.CodeServerError, false, true},
	}

	casePlans := make(map[string]map[string]any)
	casePaths := make(map[string]string)
	for _, tc := range cases {
		runID := strings.ReplaceAll(uuid.NewString(), "-", "")
		journal := map[string]any{"run_id": runID, "case": tc.name, "database": strings.TrimPrefix(parsed.Path, "/"), "listener": address, "tables": []string{"t_ai_users", "t_ai_user_tenants", "t_ai_connectors", "t_ai_api_tokens"}, "rows": map[string]any{"users": ids, "memberships": []string{"member", "admin", "joined-owner", "disabled", "invited"}, "connectors": []string{"owner-connector", "foreign-connector"}}, "request": map[string]string{"actor": tc.actor, "connector": tc.id}, "phase": "registered-before-fixture-creation"}
		path := filepath.Join(reportDir, runID+".jsonl")
		file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if err != nil {
			t.Fatal(err)
		}
		raw, err := json.Marshal(journal)
		if err != nil {
			file.Close()
			t.Fatal(err)
		}
		if _, err := file.Write(append(raw, '\n')); err != nil {
			file.Close()
			t.Fatal(err)
		}
		if err := file.Close(); err != nil {
			t.Fatal(err)
		}
		casePlans[tc.name] = journal
		casePaths[tc.name] = path
	}
	if err := setup.AutoMigrate(&entity.User{}, &entity.UserTenant{}, &entity.APIToken{}, &entity.Connector{}); err != nil {
		t.Fatal(err)
	}
	active, inactive := "1", "0"
	rows := []any{}
	for _, id := range ids {
		rows = append(rows, &entity.User{ID: id, Email: id + "@example.invalid", Nickname: id, Status: &active, IsActive: true, IsAuthenticated: true})
	}
	for _, row := range rows {
		if err := setup.Create(row).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := setup.Model(&entity.User{}).Where("id = ?", "inactive-user").Update("status", inactive).Error; err != nil {
		t.Fatal(err)
	}
	if err := setup.Model(&entity.User{}).Where("id = ?", "superuser").Update("is_superuser", true).Error; err != nil {
		t.Fatal(err)
	}
	for _, rel := range []struct{ id, role, status string }{
		{"member", "normal", active}, {"admin", "admin", active}, {"joined-owner", "owner", active}, {"disabled", "normal", inactive}, {"invited", "invite", active},
	} {
		if err := setup.Create(&entity.UserTenant{ID: rel.id, UserID: rel.id, TenantID: "owner", Role: rel.role, Status: &rel.status, InvitedBy: "owner"}).Error; err != nil {
			t.Fatal(err)
		}
	}
	indexed := time.Date(2026, 10, 25, 1, 2, 3, 0, time.UTC)
	for _, id := range []string{"owner", "foreign"} {
		if err := setup.Create(&entity.Connector{ID: id + "-connector", TenantID: id, Name: id + " docs", Source: "rest-api", InputType: "poll", Config: entity.JSONMap{"credentials": map[string]any{"password": "never-expose-password", "token": "never-expose-token"}, "base_url": "https://never-connect.invalid", "cursor": "never-expose-cursor"}, RefreshFreq: 0, PruneFreq: 0, TimeoutSecs: 17, IndexingStart: &indexed, Status: "schedule"}).Error; err != nil {
			t.Fatal(err)
		}
	}
	// Application SQL is read-only at PostgreSQL level, independent of GORM callbacks.
	query := parsed.Query()
	query.Set("default_transaction_read_only", "on")
	parsed.RawQuery = query.Encode()
	runtimeDB, err := gorm.Open(postgres.Open(parsed.String()), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal(err)
	}
	runtimePool, err := runtimeDB.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { runtimePool.Close() })
	previousDB := dao.DB
	dao.DB = runtimeDB
	t.Cleanup(func() { dao.DB = previousDB })
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { readback.Close() })
	snapshot := func() string {
		t.Helper()
		var raw string
		err := readback.QueryRow(`SELECT jsonb_build_object('users',(SELECT jsonb_agg(to_jsonb(x) ORDER BY id) FROM t_ai_users x),'members',(SELECT jsonb_agg(to_jsonb(x) ORDER BY id) FROM t_ai_user_tenants x),'connectors',(SELECT jsonb_agg(to_jsonb(x) ORDER BY id) FROM t_ai_connectors x),'tokens',(SELECT jsonb_agg(to_jsonb(x)) FROM t_ai_api_tokens x))::text`).Scan(&raw)
		if err != nil {
			t.Fatal(err)
		}
		digest := sha256.Sum256([]byte(raw))
		return hex.EncodeToString(digest[:])
	}
	previousKey := server.GetSecretKey()
	configPath := os.Getenv("MULTIRAG_CONNECTOR_DETAIL_CONFIG")
	if configPath == "" {
		t.Fatal("pre-registered authentication config required")
	}
	if err := server.FromConfigFile(configPath); err != nil {
		t.Fatal(err)
	}
	if err := server.InitVariables(nil); err != nil {
		t.Fatal(err)
	}
	server.SetSecretKey("owned-connector-detail-test-signing-key")
	t.Cleanup(func() { server.SetSecretKey(previousKey) })
	previousAdmin := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned connector detail test")
	t.Cleanup(func() { local.SetAdminStatus(previousAdmin.Status, previousAdmin.Reason) })
	gin.SetMode(gin.ReleaseMode)
	users := service.NewUserService()
	app := gin.New()
	(&Router{authHandler: handler.NewAuthHandler(), connectorHandler: handler.NewConnectorHandler(service.NewConnectorService(), users)}).Setup(app)
	listener, err := net.Listen("tcp", address)
	if err != nil {
		t.Fatal(err)
	}
	live := &httptest.Server{Listener: listener, Config: &http.Server{Handler: app}}
	live.Start()
	t.Cleanup(live.Close)
	transport := http.DefaultTransport.(*http.Transport).Clone()
	client := &http.Client{Transport: transport, Timeout: 5 * time.Second}
	t.Cleanup(transport.CloseIdleConnections)
	trap := &connectorOutboundTrap{}
	previousTransport := http.DefaultTransport
	http.DefaultTransport = trap
	t.Cleanup(func() { http.DefaultTransport = previousTransport })
	var failConnectorQuery atomic.Bool
	if err := runtimeDB.Callback().Query().Before("gorm:query").Register("connector-detail-failure", func(tx *gorm.DB) {
		if failConnectorQuery.Load() && tx.Statement.Table == "t_ai_connectors" {
			tx.AddError(errors.New("never-expose-database-secret"))
		}
	}); err != nil {
		t.Fatal(err)
	}
	var deniedBody string
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			journal := casePlans[tc.name]
			path := casePaths[tc.name]
			journal["phase"] = "registered-before-request"
			write := func() {
				t.Helper()
				data, err := json.Marshal(journal)
				if err != nil {
					t.Fatal(err)
				}
				file, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY|os.O_CREATE, 0600)
				if err != nil {
					t.Fatal(err)
				}
				if _, err := file.Write(append(data, '\n')); err != nil {
					file.Close()
					t.Fatal(err)
				}
				if err := file.Close(); err != nil {
					t.Fatal(err)
				}
			}
			write()
			before := snapshot()
			startCalls := trap.calls.Load()
			failConnectorQuery.Store(tc.dbError)
			defer failConnectorQuery.Store(false)
			if tc.license {
				local.SetAdminStatus(1, "owned unavailable")
				defer local.SetAdminStatus(0, "owned connector detail test")
			}
			req, err := http.NewRequest(http.MethodGet, live.URL+"/api/v1/connectors/"+tc.id, nil)
			if err != nil {
				t.Fatal(err)
			}
			if tc.actor != "" {
				token := "invalid"
				if tc.actor != "invalid" {
					token, err = utility.SignUserJWT(tc.actor+"@example.invalid", server.GetSecretKey())
					if err != nil {
						t.Fatal(err)
					}
				}
				req.Header.Set("Authorization", "Bearer "+token)
			}
			response, err := client.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			raw, err := io.ReadAll(response.Body)
			response.Body.Close()
			if err != nil {
				t.Fatal(err)
			}
			var body map[string]any
			if err := json.Unmarshal(raw, &body); err != nil {
				t.Fatal(err)
			}
			journal["http_status"] = response.StatusCode
			journal["response"] = body
			journal["sql_before"] = before
			journal["sql_after"] = snapshot()
			journal["outbound_calls"] = trap.calls.Load() - startCalls
			journal["phase"] = "request-completed"
			write()
			if response.StatusCode != tc.status || body["code"] != float64(tc.code) {
				t.Fatalf("status=%d body=%s want status=%d code=%d", response.StatusCode, raw, tc.status, tc.code)
			}
			if before != journal["sql_after"] || trap.calls.Load() != startCalls {
				t.Fatal("GET produced SQL mutation or outbound HTTP")
			}
			for _, forbidden := range []string{`"config"`, `"credentials"`, `"password"`, `"token"`, `"cursor"`, `"base_url"`, "never-expose"} {
				if strings.Contains(string(raw), forbidden) {
					t.Fatalf("private field leaked: %s", raw)
				}
			}
			if tc.code == 0 {
				data, ok := body["data"].(map[string]any)
				if !ok {
					t.Fatalf("data=%v", body["data"])
				}
				expected := map[string]any{"id": "owner-connector", "tenant_id": "owner", "name": "owner docs", "source": "rest-api", "input_type": "poll", "refresh_freq": float64(0), "prune_freq": float64(0), "timeout_secs": float64(17), "indexing_start": "2026-10-25T01:02:03Z", "status": "schedule"}
				want, _ := json.Marshal(expected)
				got, _ := json.Marshal(data)
				if string(got) != string(want) {
					t.Fatalf("metadata=%s want=%s", got, want)
				}
			} else if tc.code == common.CodeAuthenticationError {
				if body["data"] != nil || body["message"] != "No authorization." {
					t.Fatalf("denial leaked: %s", raw)
				}
				if deniedBody == "" {
					deniedBody = string(raw)
				} else if deniedBody != string(raw) {
					t.Fatal("unknown/foreign/membership denials differ")
				}
			}
			journal["assertions_passed"] = true
			write()
		})
	}
	if err := runtimeDB.Callback().Query().Remove("connector-detail-failure"); err != nil {
		t.Fatal(err)
	}
	if !t.Failed() {
		t.Logf("validated %d real HTTP/JWT/SQL cases; request SQL read-only; zero source HTTP calls", len(cases))
	}
}
