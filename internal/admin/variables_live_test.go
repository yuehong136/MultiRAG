package admin

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/cli"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/service"
	"multirag/internal/utility"
)

func TestAdminVariablesCLIHTTPAndSQL(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_AFTER9D_B_GOVARS_DSN")
	if dsn == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned variables scratch database required")
		}
		t.Skip("requires owned variables scratch database")
	}
	parsed, err := url.Parse(dsn)
	if err != nil || !strings.HasPrefix(strings.TrimPrefix(parsed.Path, "/"), "multirag_after9d_b_govars_") {
		t.Fatal("unowned database")
	}
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	cfg := filepath.Join(t.TempDir(), "vars.yaml")
	if err := os.WriteFile(cfg, []byte("server:\n  secret_key: owned-variable-test-secret-key-32-characters\nauthentication:\n  enable_register: false\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(cfg); err != nil {
		t.Fatal(err)
	}
	if err := server.InitVariables(nil); err != nil {
		t.Fatal(err)
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal(err)
	}
	pool, _ := db.DB()
	t.Cleanup(func() { pool.Close() })
	prev := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = prev })
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { readback.Close() })
	if err := db.AutoMigrate(&entity.SystemSettings{}, &entity.User{}); err != nil {
		t.Fatal(err)
	}
	active, access := "1", "active-admin-session"
	hash, err := service.NewUserService().HashPassword("variable-password")
	if err != nil {
		t.Fatal(err)
	}
	yes, no := true, false
	for _, u := range []*entity.User{{ID: "vars-admin", Email: "vars-admin@example.invalid", Nickname: "admin", Status: &active, AccessToken: &access, IsSuperuser: &yes, Password: &hash}, {ID: "vars-normal", Email: "vars-normal@example.invalid", Nickname: "normal", Status: &active, AccessToken: &access, IsSuperuser: &no}, {ID: "vars-inactive", Email: "vars-inactive@example.invalid", Nickname: "inactive", Status: &active, AccessToken: &access, IsSuperuser: &yes}} {
		if err := db.Create(u).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := db.Model(&entity.User{}).Where("id = ?", "vars-inactive").Update("is_active", false).Error; err != nil {
		t.Fatal(err)
	}
	for _, v := range []entity.SystemSettings{{Name: "mail.server", DataType: "string", Value: "before", Source: "admin"}, {Name: "mail.port", DataType: "integer", Value: "15", Source: "admin"}, {Name: "mail.use_tls", DataType: "boolean", Value: "true", Source: "admin"}, {Name: "literal_%a", DataType: "string", Value: "literal", Source: "admin"}, {Name: "literalXXa", DataType: "string", Value: "wildcard", Source: "admin"}, {Name: "bang!_%a", DataType: "string", Value: "literal-bang", Source: "admin"}} {
		if err := db.Create(&v).Error; err != nil {
			t.Fatal(err)
		}
	}
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	NewRouter(NewHandler(NewService())).Setup(app)
	httpServer := httptest.NewServer(app)
	defer httpServer.Close()
	address, _ := url.Parse(httpServer.URL)
	port, _ := strconv.Atoi(address.Port())
	client := cli.NewMultiRAGClient("admin")
	client.HTTPClient.Host = address.Hostname()
	client.HTTPClient.Port = port
	token, err := utility.SignUserJWT("vars-admin@example.invalid", server.GetSecretKey())
	if err != nil {
		t.Fatal(err)
	}
	client.HTTPClient.LoginToken = token
	snapshot := func() string {
		t.Helper()
		var raw string
		if err := readback.QueryRow(`SELECT jsonb_agg(to_jsonb(x) ORDER BY name)::text FROM t_ai_system_settings x`).Scan(&raw); err != nil {
			t.Fatal(err)
		}
		return raw
	}
	execute := func(text string) (cli.ResponseIf, error) {
		t.Helper()
		cmd, err := cli.NewParser(text).Parse(true)
		if err != nil {
			return nil, err
		}
		return client.ExecuteCommand(cmd)
	}
	result, err := execute("LIST VARS;")
	if err != nil {
		t.Fatal(err)
	}
	rows := result.(*cli.CommonResponse).Data
	names := []string{}
	for _, r := range rows {
		names = append(names, r["name"].(string))
		if r["setting_type"] != "config" || r["source"] != nil {
			t.Fatal("bad variable row", r)
		}
	}
	if !sort.StringsAreSorted(names) {
		t.Fatal("unsorted variables")
	}
	for _, tc := range []struct{ sql, name, value string }{{"SET VAR mail.port 0;", "mail.port", "0"}, {"SET VAR mail.use_tls false;", "mail.use_tls", "false"}, {"SET VAR mail.server '';", "mail.server", ""}, {`SET VAR sandbox.owned '{"enabled":false}';`, "sandbox.owned", `{"enabled":false}`}, {"SET VAR feature.enabled false;", "feature.enabled", "false"}, {"SET VAR mail exact;", "mail", "exact"}} {
		if _, err := execute(tc.sql); err != nil {
			t.Fatal(tc.sql, err)
		}
		var value string
		if err := readback.QueryRow(`SELECT value FROM t_ai_system_settings WHERE name=$1`, tc.name).Scan(&value); err != nil || value != tc.value {
			t.Fatal("committed setting missing", tc, err)
		}
	}
	result, err = execute("SHOW VAR mail;")
	if err != nil || len(result.(*cli.CommonResponse).Data) != 1 {
		t.Fatal("exact priority failed", err)
	}
	result, err = execute("SHOW VAR mail.;")
	if err != nil || len(result.(*cli.CommonResponse).Data) != 3 {
		t.Fatal("prefix query failed", err)
	}
	cmd := cli.NewCommand("show_variable")
	cmd.Params["var_name"] = "literal_%"
	result, err = client.ExecuteCommand(cmd)
	if err != nil || len(result.(*cli.CommonResponse).Data) != 1 {
		t.Fatal("literal prefix wildcard escaped incorrectly", err)
	}
	cmd.Params["var_name"] = "bang!_%"
	result, err = client.ExecuteCommand(cmd)
	if err != nil || len(result.(*cli.CommonResponse).Data) != 1 {
		t.Fatal("literal bang prefix escaped incorrectly", err)
	}
	before := snapshot()
	for _, text := range []string{"SET VAR mail.port invalid;", "SET VAR mail.use_tls yes;", "SET VAR sandbox.new invalid;", "SET VAR new.enabled invalid;", "SHOW VAR missing;"} {
		if _, err := execute(text); err == nil {
			t.Fatal("invalid value acknowledged", text)
		}
		if snapshot() != before {
			t.Fatal("invalid value changed SQL")
		}
	}
	for _, actor := range []string{"", "invalid", "vars-normal", "vars-inactive"} {
		auth := actor
		if strings.HasPrefix(actor, "vars-") {
			auth, err = utility.SignUserJWT(actor+"@example.invalid", server.GetSecretKey())
			if err != nil {
				t.Fatal(err)
			}
		}
		request, _ := http.NewRequest("PUT", httpServer.URL+"/api/v1/admin/variables", strings.NewReader(`{"var_name":"mail.port","var_value":"42"}`))
		request.Header.Set("Authorization", auth)
		request.Header.Set("Content-Type", "application/json")
		response, err := http.DefaultClient.Do(request)
		if err != nil {
			t.Fatal(err)
		}
		response.Body.Close()
		if response.StatusCode != 401 && response.StatusCode != 403 {
			t.Fatal("permission accepted", response.StatusCode)
		}
		if snapshot() != before {
			t.Fatal("denied request changed SQL")
		}
	}
	for _, body := range []string{`{}`, `{"var_name":"mail.port"}`, `{"var_name":"mail.port","var_value":0}`, `{"var_name":"mail.use_tls","var_value":false}`, `{"var_name":"mail.server","var_value":null}`} {
		request, _ := http.NewRequest("PUT", httpServer.URL+"/api/v1/admin/variables", strings.NewReader(body))
		request.Header.Set("Authorization", token)
		request.Header.Set("Content-Type", "application/json")
		response, err := http.DefaultClient.Do(request)
		if err != nil {
			t.Fatal(err)
		}
		var out map[string]any
		json.NewDecoder(response.Body).Decode(&out)
		response.Body.Close()
		if out["code"] == float64(0) {
			t.Fatal("missing or mistyped value succeeded")
		}
		if snapshot() != before {
			t.Fatal("invalid body changed SQL")
		}
	}
	binary := os.Getenv("MULTIRAG_AFTER9D_B_CLI_BINARY")
	if binary == "" {
		t.Fatal("actual CLI binary required")
	}
	for _, tc := range []struct {
		text    string
		success bool
	}{{"SET VAR mail.server '';", true}, {"LIST VARS;", true}, {"SET VAR mail.port invalid;", false}, {"SHOW VAR missing;", false}} {
		before := snapshot()
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		output, err := exec.CommandContext(ctx, binary, "--admin", "-h", address.Host, "-u", "vars-admin@example.invalid", "-p", "variable-password", tc.text).CombinedOutput()
		cancel()
		if (err == nil) != tc.success {
			t.Fatalf("actual CLI %s: %v %s", tc.text, err, output)
		}
		if !tc.success && snapshot() != before {
			t.Fatal("failed CLI mutated settings")
		}
	}
}
