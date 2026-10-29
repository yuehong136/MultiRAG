package router_test

import (
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
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/admin"
	"multirag/internal/cli"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/testsupport"
	"multirag/internal/utility"
)

// Real existing user/admin middleware resolves JWTs against scratch PostgreSQL.
// Parser -> ExecuteCommand -> real TCP HTTP -> actual protected routers -> static
// catalog is exercised in both modes; independent SQL snapshots prove no writes.
// This test never calls a real supplier and does not initialize other backends.
func TestCatalogActualAuthHTTPAndSQL(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_AFTER7DD_CATALOG_DSN")
	if dsn == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned catalog PostgreSQL required")
		}
		t.Skip("requires owned catalog PostgreSQL")
	}
	u, err := url.Parse(dsn)
	if err != nil || u.Scheme != "postgres" || u.Hostname() != "127.0.0.1" || u.Port() == "" || !strings.HasPrefix(strings.TrimPrefix(u.Path, "/"), "multirag_") || strings.TrimPrefix(u.Path, "/") != os.Getenv("MULTIRAG_AFTER7DD_CATALOG_DB") {
		t.Fatal("explicit owned TCP database required")
	}
	if os.Getenv("MULTIRAG_RUN_ID") == "" {
		t.Fatal("registered runner required")
	}
	j := testsupport.NewCase(t)
	reader, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("open independent SQL")
	}
	t.Cleanup(func() { reader.Close() })
	if reader.PingContext(t.Context()) != nil {
		t.Fatal("independent SQL unavailable")
	}
	schema := "catalog_auth_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	var absent bool
	if err := reader.QueryRowContext(t.Context(), "SELECT NOT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=$1)", schema).Scan(&absent); err != nil || !absent {
		t.Fatal("scratch schema precondition")
	}
	j.Event("reserve-sql-schema", map[string]any{"endpoint": u.Host, "database": strings.TrimPrefix(u.Path, "/"), "schema": schema, "tables": []string{"t_ai_users", "t_ai_api_tokens"}})
	if _, err := reader.ExecContext(t.Context(), `CREATE SCHEMA "`+schema+`"`); err != nil {
		t.Fatal("create owned schema")
	}
	t.Cleanup(func() {
		if _, err := reader.ExecContext(context.Background(), `DROP SCHEMA "`+schema+`" CASCADE`); err != nil {
			t.Error("drop owned schema failed")
			return
		}
		var count int
		err := reader.QueryRowContext(context.Background(), "SELECT count(*) FROM pg_namespace WHERE nspname=$1", schema).Scan(&count)
		j.Event("schema-cleanup-readback", map[string]any{"schema": schema, "absent": err == nil && count == 0})
		if err != nil || count != 0 {
			t.Error("owned schema survived")
		}
	})
	scoped := *u
	query := scoped.Query()
	query.Set("search_path", schema)
	scoped.RawQuery = query.Encode()
	db, err := gorm.Open(postgres.Open(scoped.String()), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("GORM scratch connect")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal("scratch pool")
	}
	t.Cleanup(func() { pool.Close() })
	oldDB := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = oldDB })
	if err := db.AutoMigrate(&entity.User{}, &entity.APIToken{}); err != nil {
		t.Fatal("minimal scratch schema migration")
	}

	writePrivate := func(name string, body []byte) string {
		t.Helper()
		path := filepath.Join(os.Getenv("TMPDIR"), schema+"-"+name)
		j.Event("reserve-private-file", map[string]any{"path": path, "sha256": fmt.Sprintf("%x", sha256.Sum256(body))})
		f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if err != nil {
			t.Fatal("create owned file")
		}
		t.Cleanup(func() {
			if err := os.Remove(path); err != nil {
				t.Error("remove owned file")
			}
			_, err := os.Lstat(path)
			j.Event("file-cleanup-readback", map[string]any{"path": path, "absent": os.IsNotExist(err)})
			if !os.IsNotExist(err) {
				t.Error("owned file survived")
			}
		})
		if _, err = f.Write(body); err != nil {
			f.Close()
			t.Fatal("write owned file")
		}
		if err = f.Sync(); err != nil {
			f.Close()
			t.Fatal("sync owned file")
		}
		if err = f.Close(); err != nil {
			t.Fatal("close owned file")
		}
		stored, err := os.ReadFile(path)
		if err != nil || string(stored) != string(body) {
			t.Fatal("owned file independent readback")
		}
		return path
	}
	// Use the configured explicit signing key and existing server variable setup.
	// Running this case alone keeps the global test configuration isolated.
	if err := common.Init("error"); err != nil {
		t.Fatal("initialize stdout logger")
	}
	cfg := writePrivate("server.yaml", []byte("multirag:\n  secret_key: catalog-owned-test-signing-key-32-characters\n"))
	if err := server.FromConfigFile(cfg); err != nil {
		t.Fatal("private config load")
	}
	if err := server.InitVariables(nil); err != nil {
		t.Fatal("signing variables")
	}
	catalogPath := writePrivate("catalog.json", []byte(`[{"name":"canonical","alias":["组织/a+b?"],"max_tokens":0,"model_types":["embedding"]}]`))
	manager := &entity.ProviderManager{}
	if err := manager.LoadAllModels(catalogPath); err != nil {
		t.Fatal("static catalog load")
	}
	modelService := service.NewModelProviderServiceWithCatalog(manager, nil)
	modelHandler := handler.NewModelHandler(modelService)
	active, inactive := "1", "0"
	tokens := map[string]string{}
	ids := map[string]string{}
	j.Event("reserve-sql-identities", map[string]any{"schema": schema, "users": []string{"ordinary", "administrator", "inactive"}, "providerIO": "none"})
	for _, name := range []string{"ordinary", "administrator", "inactive"} {
		id := strings.ReplaceAll(uuid.NewString(), "-", "")
		ids[name] = id
		isAdmin := name == "administrator"
		status := active
		if name == "inactive" {
			status = inactive
		}
		email := id + "@catalog.invalid"
		session := "catalog-owned-session"
		j.Event("reserve-auth-row", map[string]any{"schema": schema, "id": id, "role": name, "email": email})
		if err := db.Create(&entity.User{ID: id, Email: email, Nickname: name, Status: &status, AccessToken: &session, IsActive: true, IsAuthenticated: true, IsSuperuser: &isAdmin}).Error; err != nil {
			t.Fatal("seed auth user")
		}
		token, err := utility.SignUserJWT(email, server.GetSecretKey())
		if err != nil {
			t.Fatal("sign user JWT")
		}
		tokens[name] = token
	}
	snapshot := func() string {
		t.Helper()
		parts := []string{}
		for _, table := range []string{"t_ai_users", "t_ai_api_tokens"} {
			var value string
			if err := reader.QueryRowContext(t.Context(), `SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb)::text FROM "`+schema+`"."`+table+`" t`).Scan(&value); err != nil {
				t.Fatal("independent SQL snapshot")
			}
			parts = append(parts, value)
		}
		return strings.Join(parts, "\n")
	}
	before := snapshot()
	oldLicense := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned catalog test")
	t.Cleanup(func() { local.SetAdminStatus(oldLicense.Status, oldLicense.Reason) })
	gin.SetMode(gin.ReleaseMode)
	userService := service.NewUserService()
	userRouter := router.NewRouter(handler.NewAuthHandler(), handler.NewUserHandler(userService), nil, handler.NewDocumentHandler(service.NewDocumentService(nil)), nil, handler.NewSystemHandler(service.NewSystemService()), nil, nil, nil, nil, handler.NewConnectorHandler(service.NewConnectorService(nil), userService), nil, nil, nil, nil, nil)
	userRouter.SetModelHandler(modelHandler)
	userEngine := gin.New()
	userRouter.Setup(userEngine)
	adminRouter := admin.NewRouter(admin.NewHandler(admin.NewService()))
	adminRouter.SetModelHandler(modelHandler) // I adds this mirror of user setter.
	adminEngine := gin.New()
	adminRouter.Setup(adminEngine)
	start := func(address string, h http.Handler) *httptest.Server {
		t.Helper()
		host, port, err := net.SplitHostPort(address)
		n, e := strconv.Atoi(port)
		if err != nil || e != nil || host != "127.0.0.1" || n <= 0 {
			t.Fatal("exact registered listener required")
		}
		j.Event("reserve-listener", map[string]any{"address": address})
		l, err := net.Listen("tcp", address)
		if err != nil {
			t.Fatal("bind owned listener")
		}
		srv := &httptest.Server{Listener: l, Config: &http.Server{Handler: h}}
		srv.Start()
		t.Cleanup(func() {
			srv.Close()
			conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
			if conn != nil {
				conn.Close()
			}
			j.Event("listener-cleanup-readback", map[string]any{"address": address, "absent": err != nil})
			if err == nil {
				t.Error("listener survived")
			}
		})
		return srv
	}
	userHTTP := start(os.Getenv("MULTIRAG_AFTER7DD_CATALOG_USER_ADDR"), userEngine)
	adminHTTP := start(os.Getenv("MULTIRAG_AFTER7DD_CATALOG_ADMIN_ADDR"), adminEngine)
	// Assert the actual existing mode separation, with both success and denial.
	for _, surface := range []struct {
		base, prefix string
		admin        bool
	}{{userHTTP.URL, "/api/v1", false}, {adminHTTP.URL, "/api/v1/admin", true}} {
		for _, identity := range []string{"", "invalid", "ordinary", "administrator", "inactive"} {
			want := http.StatusOK
			if identity == "" || identity == "invalid" || identity == "inactive" {
				want = http.StatusUnauthorized
			} else if (identity == "administrator") != surface.admin {
				want = http.StatusForbidden
			}
			for _, path := range []string{"/all-models", "/all-models/detail?model_name=" + url.QueryEscape("组织/a+b?")} {
				req, err := http.NewRequestWithContext(t.Context(), "GET", surface.base+surface.prefix+path, nil)
				if err != nil {
					t.Fatal(err)
				}
				if identity != "" {
					token := tokens[identity]
					if identity == "invalid" {
						token = "invalid-catalog-jwt"
					}
					req.Header.Set("Authorization", token)
				}
				response, err := http.DefaultClient.Do(req)
				if err != nil {
					t.Fatal("actual auth request")
				}
				raw, readErr := io.ReadAll(io.LimitReader(response.Body, 1<<20))
				response.Body.Close()
				if readErr != nil {
					t.Fatal("read auth response")
				}
				var body struct {
					Code int             `json:"code"`
					Data json.RawMessage `json:"data"`
				}
				if json.Unmarshal(raw, &body) != nil || response.StatusCode != want || (want == 200) != (body.Code == 0) {
					t.Fatalf("surface admin=%v identity=%s status=%d code=%d", surface.admin, identity, response.StatusCode, body.Code)
				}
				if want != 200 && string(body.Data) != "" && string(body.Data) != "null" {
					t.Fatal("denied request exposed metadata")
				}
			}
		}
	}
	// These are the production compiled CLI parser, command dispatcher and HTTP
	// client, pointed at the actual auth routers (not synthetic identity fixtures).
	for _, surface := range []struct{ mode, base, identity string }{{"user", userHTTP.URL, "ordinary"}, {"admin", adminHTTP.URL, "administrator"}} {
		parsed, _ := url.Parse(surface.base)
		port, _ := strconv.Atoi(parsed.Port())
		client := cli.NewMultiRAGClient(surface.mode)
		client.HTTPClient.Host = parsed.Hostname()
		client.HTTPClient.Port = port
		client.HTTPClient.LoginToken = tokens[surface.identity]
		for _, syntax := range []string{`LIST MODELS;`, `SHOW MODEL '组织/a+b?';`} {
			command, err := cli.NewParser(syntax).Parse(surface.mode == "admin")
			if err != nil {
				t.Fatal("actual CLI parse")
			}
			result, err := client.ExecuteCommand(command)
			if err != nil {
				t.Fatal("actual CLI HTTP dispatch")
			}
			switch typed := result.(type) {
			case *cli.CommonResponse:
				if len(typed.Data) != 1 || typed.Data[0]["name"] != "canonical" || typed.Data[0]["max_tokens"] != float64(0) {
					t.Fatal("CLI list metadata mismatch")
				}
			case *cli.CommonDataResponse:
				if typed.Data["name"] != "canonical" {
					t.Fatal("CLI alias mismatch")
				}
			default:
				t.Fatal("unexpected actual CLI result")
			}
		}
		client.HTTPClient.LoginToken = tokens[map[string]string{"user": "administrator", "admin": "ordinary"}[surface.mode]]
		denied, _ := cli.NewParser(`LIST MODELS;`).Parse(surface.mode == "admin")
		if _, err := client.ExecuteCommand(denied); err == nil {
			t.Fatal("CLI bypassed actual mode boundary")
		}
	}
	if after := snapshot(); after != before {
		t.Fatal("catalog or auth requests wrote SQL")
	}
	j.Event("independent-sql-readback", map[string]any{"schema": schema, "unchanged": true, "userCount": len(ids), "catalogCount": 1, "realVendor": false, "actualAuth": true, "actualCLIHTTP": true})
}
