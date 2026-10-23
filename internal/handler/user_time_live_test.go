package handler_test

import (
	"bytes"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"golang.org/x/crypto/bcrypt"
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
	"multirag/internal/utility"
)

// Real registered routes, RSA/CLI auth, and PostgreSQL; no identity middleware stub.
func TestUserTimeHTTPCLIAndSQL(t *testing.T) {
	t.Log("owned-test-process", os.Getpid())
	dsn, config := os.Getenv("MULTIRAG_GO_TIME_DSN"), os.Getenv("MULTIRAG_GO_USER_CONFIG")
	if dsn == "" || config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned time PostgreSQL DSN and config required")
		}
		t.Skip("requires owned time-user PostgreSQL")
	}
	parsed, err := url.Parse(dsn)
	if err != nil || !strings.HasPrefix(strings.TrimPrefix(parsed.Path, "/"), "multirag_after1c_a_time") {
		t.Fatal("unowned scratch database")
	}
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal("load owned config")
	}
	if err := server.InitVariables(nil); err != nil {
		t.Fatal(err)
	}
	server.SetSecretKey(server.GetConfig().Server.SecretKey)
	local.InitAdminStatus(0, "owned user-time acceptance")
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("connect scratch PostgreSQL")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := pool.Close(); err != nil {
			t.Error(err)
		}
	})
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := readback.Close(); err != nil {
			t.Error(err)
		}
	})
	previous := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = previous })
	if err := db.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.File{}, &entity.APIToken{}, &entity.Document{}); err != nil {
		t.Fatal(err)
	}
	if err := db.Exec(`DROP TRIGGER IF EXISTS owned_login_audit ON t_ai_users; DROP TRIGGER IF EXISTS owned_login_reject ON t_ai_users; TRUNCATE t_ai_users,t_ai_tenants,t_ai_user_tenants,t_ai_files,t_ai_api_tokens,t_ai_documents; CREATE TABLE IF NOT EXISTS owned_login_writes(id text, token_changed boolean, login_changed boolean); TRUNCATE owned_login_writes;
 CREATE OR REPLACE FUNCTION owned_audit_login() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN INSERT INTO owned_login_writes VALUES(NEW.id,NEW.access_token IS DISTINCT FROM OLD.access_token,NEW.last_login_time IS DISTINCT FROM OLD.last_login_time); RETURN NEW; END $$;
 CREATE TRIGGER owned_login_audit AFTER UPDATE ON t_ai_users FOR EACH ROW EXECUTE FUNCTION owned_audit_login();`).Error; err != nil {
		t.Fatal(err)
	}
	password := "owned-time-password"
	hash, err := bcrypt.GenerateFromPassword([]byte(password), bcrypt.MinCost)
	if err != nil {
		t.Fatal(err)
	}
	active := "1"
	epoch := time.Unix(0, 0).UTC()
	for _, id := range []string{"owner", "foreign", "disabled", "external", "write-fail"} {
		token := "owned-initial-access-token-" + id
		user := &entity.User{ID: id, Email: id + "@time.invalid", Nickname: id, Password: stringPointer(string(hash)), AccessToken: &token, Status: &active, IsActive: true, LastLoginTime: &epoch}
		if id == "external" {
			user.AccountKind = "external"
		}
		if err := db.Create(user).Error; err != nil {
			t.Fatal(err)
		}
	}
	if _, err := readback.Exec("UPDATE t_ai_users SET is_active=false WHERE id='disabled'; TRUNCATE owned_login_writes"); err != nil {
		t.Fatal(err)
	}
	svc := service.NewUserService()
	userHandler := handler.NewUserHandler(svc)
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	app.Use(gin.Recovery())
	routes := router.NewRouter(handler.NewAuthHandler(), userHandler, nil, nil, nil, handler.NewSystemHandler(service.NewSystemService()), nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil)
	routes.Setup(app)
	api := httptest.NewServer(app)
	t.Cleanup(func() { api.Close(); t.Log("owned-http-resource-closed", api.URL) })
	t.Log("owned-http-resource", api.URL)
	type response struct {
		status, code int
		data         map[string]any
		token        string
	}
	call := func(method, path, token string, body any) response {
		t.Helper()
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		req, err := http.NewRequest(method, api.URL+path, bytes.NewReader(raw))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Content-Type", "application/json")
		if token != "" {
			req.Header.Set("Authorization", "Bearer "+token)
		}
		resp, err := api.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var out struct {
			Code *int            `json:"code"`
			Data json.RawMessage `json:"data"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
			t.Fatal(err)
		}
		if out.Code == nil {
			t.Fatal("missing business code")
		}
		var data map[string]any
		if len(out.Data) > 0 && out.Data[0] == '{' {
			if err := json.Unmarshal(out.Data, &data); err != nil {
				t.Fatal(err)
			}
		}
		return response{status: resp.StatusCode, code: *out.Code, data: data, token: resp.Header.Get("Authorization")}
	}
	snapshot := func(id string) string {
		t.Helper()
		var raw string
		if err := readback.QueryRow("SELECT to_jsonb(u)::text FROM t_ai_users u WHERE id=$1", id).Scan(&raw); err != nil {
			t.Fatal(err)
		}
		return raw
	}
	writes := func(id string) int {
		t.Helper()
		var n int
		if err := readback.QueryRow("SELECT count(*) FROM owned_login_writes WHERE id=$1", id).Scan(&n); err != nil {
			t.Fatal(err)
		}
		return n
	}
	cipher := func(plain string) string {
		t.Helper()
		encrypted, err := cli.EncryptPassword(plain)
		if err != nil {
			t.Fatal(err)
		}
		return encrypted
	}
	checkLogin := func(id string, before time.Time, oldToken string, oldCreate int64, oldDate time.Time) {
		t.Helper()
		var token string
		var login, updated, created time.Time
		var update, create int64
		if err := readback.QueryRow("SELECT access_token,last_login_time,update_time,update_date,create_time,create_date FROM t_ai_users WHERE id=$1", id).Scan(&token, &login, &update, &updated, &create, &created); err != nil {
			t.Fatal(err)
		}
		if token == oldToken || login.Before(before.Truncate(time.Second)) || login.After(time.Now()) || login.Nanosecond() != 0 || update < before.UnixMilli() || update > time.Now().UnixMilli() || updated.Unix() != update/1000 || create != oldCreate || !created.Equal(oldDate) {
			t.Fatal("login timestamp/token/creation SQL contract failed")
		}
		var tokenChanged, loginChanged bool
		if err := readback.QueryRow("SELECT token_changed,login_changed FROM owned_login_writes WHERE id=$1 ORDER BY ctid DESC LIMIT 1", id).Scan(&tokenChanged, &loginChanged); err != nil {
			t.Fatal(err)
		}
		if !tokenChanged || !loginChanged {
			t.Fatal("token and login time were not written together")
		}
	}
	readCreation := func(id string) (string, int64, time.Time) {
		t.Helper()
		var token string
		var create int64
		var date time.Time
		if err := readback.QueryRow("SELECT access_token,create_time,create_date FROM t_ai_users WHERE id=$1", id).Scan(&token, &create, &date); err != nil {
			t.Fatal(err)
		}
		return token, create, date
	}
	var ownerToken string
	t.Run("http-login-profile", func(t *testing.T) {
		oldToken, create, date := readCreation("owner")
		before := time.Now()
		got := call("POST", "/api/v1/auth/login", "", map[string]any{"username": "owner@time.invalid", "password": cipher(password)})
		if got.status != http.StatusOK || got.code != int(common.CodeSuccess) || got.token == "" {
			t.Fatal("login failed")
		}
		ownerToken = got.token
		checkLogin("owner", before, oldToken, create, date)
		if writes("owner") != 1 {
			t.Fatal("login must perform one SQL mutation")
		}
		profile := call("GET", "/api/v1/users/me", ownerToken, nil)
		if profile.code != 0 || profile.data["last_login_time"] != got.data["last_login_time"] || profile.data["id"] != "owner" {
			t.Fatal("profile login readback differs")
		}
	})
	t.Run("email-service-login", func(t *testing.T) {
		oldToken, create, date := readCreation("foreign")
		before := time.Now()
		user, code, err := svc.LoginByEmail(&service.EmailLoginRequest{Email: "foreign@time.invalid", Password: cipher(password)})
		if err != nil || code != common.CodeSuccess || user == nil {
			t.Fatal("email login failed")
		}
		checkLogin("foreign", before, oldToken, create, date)
		if writes("foreign") != 1 {
			t.Fatal("email login must perform one SQL mutation")
		}
	})
	t.Run("denied-login-zero-side-effects", func(t *testing.T) {
		for _, id := range []string{"owner", "disabled", "external"} {
			old, n := snapshot(id), writes(id)
			input := password
			if id == "owner" {
				input = "wrong-password"
			}
			got := call("POST", "/api/v1/auth/login", "", map[string]any{"username": id + "@time.invalid", "password": cipher(input)})
			if got.code == 0 || got.token != "" || snapshot(id) != old || writes(id) != n {
				t.Fatal("failed HTTP login mutated user")
			}
			if user, code, err := svc.LoginByEmail(&service.EmailLoginRequest{Email: id + "@time.invalid", Password: cipher(input)}); err == nil || code == 0 || user != nil || snapshot(id) != old || writes(id) != n {
				t.Fatal("failed email login mutated user")
			}
		}
		for _, input := range []string{"not-base64", cipher("wrong-password")} {
			old, n := snapshot("owner"), writes("owner")
			got := call("POST", "/api/v1/auth/login", "", map[string]any{"username": "owner@time.invalid", "password": input})
			if got.code == 0 || snapshot("owner") != old || writes("owner") != n {
				t.Fatal("malformed credential mutated user")
			}
		}
		server.GetConfig().Authentication.DisablePasswordLogin = true
		t.Cleanup(func() { server.GetConfig().Authentication.DisablePasswordLogin = false })
		old, n := snapshot("owner"), writes("owner")
		got := call("POST", "/api/v1/auth/login", "", map[string]any{"username": "owner@time.invalid", "password": cipher(password)})
		if got.code != int(common.CodeForbidden) || snapshot("owner") != old || writes("owner") != n {
			t.Fatal("disabled password login mutated user")
		}
		if user, code, err := svc.LoginByEmail(&service.EmailLoginRequest{Email: "owner@time.invalid", Password: cipher(password)}); err == nil || code != common.CodeForbidden || user != nil || snapshot("owner") != old {
			t.Fatal("disabled email login mutated user")
		}
		server.GetConfig().Authentication.DisablePasswordLogin = false
	})
	t.Run("database-reject-zero-side-effects", func(t *testing.T) {
		if _, err := readback.Exec(`CREATE OR REPLACE FUNCTION owned_reject_login() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.id='write-fail' THEN RAISE EXCEPTION 'owned write rejection'; END IF; RETURN NEW; END $$; CREATE TRIGGER owned_login_reject BEFORE UPDATE ON t_ai_users FOR EACH ROW EXECUTE FUNCTION owned_reject_login()`); err != nil {
			t.Fatal(err)
		}
		old, n := snapshot("write-fail"), writes("write-fail")
		got := call("POST", "/api/v1/auth/login", "", map[string]any{"username": "write-fail@time.invalid", "password": cipher(password)})
		if got.code != int(common.CodeServerError) || got.token != "" || snapshot("write-fail") != old || writes("write-fail") != n {
			t.Fatal("failed database login partially persisted")
		}
		if user, code, err := svc.LoginByEmail(&service.EmailLoginRequest{Email: "write-fail@time.invalid", Password: cipher(password)}); err == nil || code != common.CodeServerError || user != nil || snapshot("write-fail") != old || writes("write-fail") != n {
			t.Fatal("failed email database login partially persisted")
		}
	})
	runCLI := func(process *exec.Cmd) error {
		t.Helper()
		var output bytes.Buffer
		process.Stdout, process.Stderr = &output, &output
		if err := process.Start(); err != nil {
			return err
		}
		t.Log("owned-cli-process-open", process.Process.Pid)
		err := process.Wait()
		t.Log("owned-cli-process-closed", process.Process.Pid, process.ProcessState.ExitCode())
		return err
	}
	t.Run("actual-cli-login", func(t *testing.T) {
		binary := os.Getenv("MULTIRAG_GO_TIME_CLI")
		if binary == "" {
			t.Fatal("candidate CLI binary required")
		}
		raw, err := os.ReadFile(binary)
		if err != nil {
			t.Fatal(err)
		}
		t.Logf("candidate-cli-sha256 %x", sha256.Sum256(raw))
		parsed, err := url.Parse(api.URL)
		if err != nil {
			t.Fatal(err)
		}
		oldToken, create, date := readCreation("owner")
		oldWrites := writes("owner")
		if _, err := readback.Exec("UPDATE t_ai_users SET last_login_time=$1 WHERE id='owner'", epoch); err != nil {
			t.Fatal(err)
		}
		oldWrites++
		before := time.Now()
		process := exec.CommandContext(t.Context(), binary, "-h", parsed.Host, "-o", "json", "LOGIN USER 'owner@time.invalid' WITH PASSWORD '"+password+"';")
		if err := runCLI(process); err != nil {
			t.Fatal("candidate CLI login failed", err)
		}
		checkLogin("owner", before, oldToken, create, date)
		if writes("owner") != oldWrites+1 {
			t.Fatal("CLI login mutated more than once")
		}
		old, n := snapshot("owner"), writes("owner")
		process = exec.CommandContext(t.Context(), binary, "-h", parsed.Host, "-o", "json", "LOGIN USER 'owner@time.invalid' WITH PASSWORD 'wrong-password';")
		if err := runCLI(process); err == nil || snapshot("owner") != old || writes("owner") != n {
			t.Fatal("CLI login failure lost exit/SQL contract")
		}
		client := cli.NewMultiRAGClient("user")
		port, err := strconv.Atoi(parsed.Port())
		if err != nil {
			t.Fatal(err)
		}
		client.HTTPClient.Host = parsed.Hostname()
		client.HTTPClient.Port = port
		command, err := cli.NewParser("LOGIN USER 'owner@time.invalid' WITH PASSWORD '" + password + "';").Parse(false)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := client.ExecuteCommand(command); err != nil {
			t.Fatal(err)
		}
		if client.HTTPClient.LoginToken == "" {
			t.Fatal("CLI session token missing")
		}
	})

	t.Run("settings-presence-empty-identity", func(t *testing.T) {
		type settings struct {
			avatar, language, color, timezone sql.NullString
			create, update                    int64
			created, updated                  time.Time
		}
		read := func(id string) settings {
			t.Helper()
			var row settings
			if err := readback.QueryRow("SELECT avatar,language,color_schema,timezone,create_time,update_time,create_date,update_date FROM t_ai_users WHERE id=$1", id).Scan(&row.avatar, &row.language, &row.color, &row.timezone, &row.create, &row.update, &row.created, &row.updated); err != nil {
				t.Fatal(err)
			}
			return row
		}
		old := read("owner")
		foreign := snapshot("foreign")
		before := time.Now()
		patch := map[string]any{"avatar": "owned-avatar", "language": "Chinese", "color_schema": "Dark", "timezone": "UTC", "id": "foreign", "user_id": "foreign"}
		got := call("PATCH", "/api/v1/users/me", ownerToken, patch)
		if got.status != 200 || got.code != 0 {
			t.Fatal("settings update failed")
		}
		row := read("owner")
		if row.avatar.String != "owned-avatar" || row.language.String != "Chinese" || row.color.String != "Dark" || row.timezone.String != "UTC" || row.create != old.create || !row.created.Equal(old.created) || row.update < before.UnixMilli() || row.updated.Unix() != row.update/1000 || snapshot("foreign") != foreign {
			t.Fatal("settings SQL/identity/time mismatch")
		}
		profile := call("GET", "/api/v1/users/me", ownerToken, nil)
		for _, key := range []string{"avatar", "language", "color_schema", "timezone"} {
			if profile.data[key] != patch[key] {
				t.Fatal("profile settings differ from SQL", key)
			}
		}
		got = call("PATCH", "/api/v1/users/me", ownerToken, map[string]any{"nickname": "renamed", "language": nil})
		if got.code != 0 {
			t.Fatal("omitted settings update failed")
		}
		omitted := read("owner")
		if omitted.avatar != row.avatar || omitted.language != row.language || omitted.color != row.color || omitted.timezone != row.timezone {
			t.Fatal("omitted/null settings were cleared")
		}
		got = call("POST", "/v1/user/setting", ownerToken, map[string]any{"avatar": "", "language": "", "color_schema": "", "timezone": ""})
		if got.code != 0 {
			t.Fatal("legacy settings update failed")
		}
		cleared := read("owner")
		for _, value := range []sql.NullString{cleared.avatar, cleared.language, cleared.color, cleared.timezone} {
			if !value.Valid || value.String != "" {
				t.Fatal("explicit empty setting was ignored or stored NULL")
			}
		}
		// Profile retains its established defaults for empty language/theme/timezone.
		profile = call("GET", "/api/v1/users/me", ownerToken, nil)
		if profile.data["avatar"] != "" || profile.data["language"] != "English" || profile.data["color_schema"] != "Bright" || profile.data["timezone"] != "UTC+8\tAsia/Shanghai" {
			t.Fatal("existing profile empty-value defaults changed")
		}
		for _, token := range []string{"", "invalid-jwt"} {
			old := snapshot("owner")
			got := call("PATCH", "/api/v1/users/me", token, patch)
			if got.status != 401 || got.code == 0 || snapshot("owner") != old || snapshot("foreign") != foreign {
				t.Fatal("unauthorized settings mutation")
			}
		}
		oldWrite := snapshot("write-fail")
		token, err := utility.SignUserJWT("write-fail@time.invalid", server.GetSecretKey())
		if err != nil {
			t.Fatal(err)
		}
		got = call("PATCH", "/api/v1/users/me", token, patch)
		if got.code != int(common.CodeServerError) || snapshot("write-fail") != oldWrite {
			t.Fatal("failed settings write changed SQL")
		}
	})
	t.Run("shared-producers-time-readback", func(t *testing.T) {
		before := time.Now()
		got := call("POST", "/api/v1/users", "", map[string]any{"email": "registered@time.invalid", "nickname": "registered", "password": cipher(password)})
		if got.status != 200 || got.code != 0 {
			t.Fatal("actual user registration failed")
		}
		registeredID, ok := got.data["id"].(string)
		if !ok || registeredID == "" {
			t.Fatal("registered identity missing")
		}
		check := func(table, key, id string) {
			t.Helper()
			var create, update int64
			var created, updated time.Time
			if err := readback.QueryRow("SELECT create_time,update_time,create_date,update_date FROM "+table+" WHERE "+key+"=$1", id).Scan(&create, &update, &created, &updated); err != nil {
				t.Fatal(err)
			}
			if create < before.UnixMilli() || create > time.Now().UnixMilli() || update < create || created.Unix() != create/1000 || updated.Unix() != update/1000 || created.Nanosecond() != 0 || updated.Nanosecond() != 0 {
				t.Fatal("shared producer used wrong units", table)
			}
		}
		check("t_ai_users", "id", registeredID)
		check("t_ai_tenants", "id", registeredID)
		check("t_ai_user_tenants", "user_id", registeredID)
		check("t_ai_files", "tenant_id", registeredID)
		var login sql.NullTime
		if err := readback.QueryRow("SELECT last_login_time FROM t_ai_users WHERE id=$1", registeredID).Scan(&login); err != nil {
			t.Fatal(err)
		}
		if login.Valid {
			t.Fatal("registration must not fabricate a successful login")
		}
		// Admin creation uses a transaction spanning these same shared models.
		created, err := admin.NewService().CreateUser("admin-created@time.invalid", cipher(password), "user")
		if err != nil {
			t.Fatal("admin user creation failed", err)
		}
		adminID, ok := created["id"].(string)
		if !ok {
			t.Fatal("admin-created identity missing")
		}
		check("t_ai_users", "id", adminID)
		check("t_ai_tenants", "id", adminID)
		check("t_ai_user_tenants", "user_id", adminID)
		check("t_ai_files", "tenant_id", adminID)
		apiToken, err := service.NewSystemService().CreateAPIToken(registeredID, &service.CreateAPITokenRequest{})
		if err != nil {
			t.Fatal(err)
		}
		check("t_ai_api_tokens", "token", apiToken.Token)
		if err := db.AutoMigrate(&entity.TenantModelProvider{}, &entity.TenantModelInstance{}); err != nil {
			t.Fatal(err)
		}
		if err := db.Exec("DELETE FROM tenant_model_instance WHERE provider_id='owned-time-provider'; DELETE FROM tenant_model_provider WHERE id='owned-time-provider'").Error; err != nil {
			t.Fatal(err)
		}
		if err := db.Create(&entity.TenantModelProvider{ID: "owned-time-provider", ProviderName: "owned", TenantID: registeredID}).Error; err != nil {
			t.Fatal(err)
		}
		if err := db.Create(&entity.TenantModelInstance{ID: "owned-time-instance", ProviderID: "owned-time-provider", InstanceName: "owned", APIKey: "owned-only-fixture", Status: "active"}).Error; err != nil {
			t.Fatal(err)
		}
		check("tenant_model_provider", "id", "owned-time-provider")
		check("tenant_model_instance", "id", "owned-time-instance")
	})

}

func stringPointer(value string) *string { return &value }
