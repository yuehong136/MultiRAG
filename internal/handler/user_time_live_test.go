package handler

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"multirag/internal/cli"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/service"
)

func TestUserTimeHTTPCLIAndSQL(t *testing.T) {
	_, source, _, _ := runtime.Caller(0)
	t.Chdir(filepath.Join(filepath.Dir(source), "..", ".."))
	f := newCanonicalHTTPFixture(t)
	keyRoot := t.TempDir()
	keyDirectory := filepath.Join(keyRoot, "conf")
	f.Journal.Event("reserve-canonical-login-keypair", map[string]any{"directory": keyDirectory, "public": filepath.Join(keyDirectory, "public.pem"), "private": filepath.Join(keyDirectory, "private.pem")})
	if err := os.Mkdir(keyDirectory, 0700); err != nil {
		t.Fatal(err)
	}
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	public, err := x509.MarshalPKIXPublicKey(&key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(keyDirectory, "private.pem"), pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)}), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(keyDirectory, "public.pem"), pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: public}), 0600); err != nil {
		t.Fatal(err)
	}
	t.Chdir(keyRoot)

	svc := service.NewUserService()
	h := NewUserHandler(svc)
	password := "owned-credential"
	hash, err := svc.HashPassword(base64.StdEncoding.EncodeToString([]byte(password)))
	if err != nil {
		t.Fatal(err)
	}
	for _, actor := range []string{"owner", "member", "invite", "disabled"} {
		if err := f.DB.Model(&entity.User{}).Where("id=?", f.ids[actor]).Update("password", hash).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := f.DB.Model(&entity.User{}).Where("id=?", f.ids["member"]).Update("is_active", "0").Error; err != nil {
		t.Fatal(err)
	}
	if err := f.DB.Model(&entity.User{}).Where("id=?", f.ids["invite"]).Update("status", "0").Error; err != nil {
		t.Fatal(err)
	}
	app := gin.New()
	app.POST("/api/v1/auth/login", h.LoginByEmail)
	app.POST("/api/v1/users/login", h.Login)
	app.GET("/api/v1/system/ping", NewSystemHandler(service.NewSystemService()).Ping)
	authenticated := app.Group("/api/v1", NewAuthHandler().AuthMiddleware())
	authenticated.GET("/users/me", h.Info)
	authenticated.PATCH("/users/me", h.Setting)
	urlString := f.serve(t, app)
	cipher := func(value string) string {
		t.Helper()
		encrypted, err := cli.EncryptPassword(value)
		if err != nil {
			t.Fatal(err)
		}
		return encrypted
	}
	snapshot := func(actor string) string {
		t.Helper()
		var raw string
		if err := f.Read.QueryRow("SELECT concat(access_token,'|',coalesce(cast(last_login_time as char),''),'|',coalesce(cast(update_time as char),'')) FROM user WHERE id=?", f.ids[actor]).Scan(&raw); err != nil {
			t.Fatal(err)
		}
		return raw
	}
	old := snapshot("owner")
	before := time.Now()
	status, result, raw, headers := f.request(t, urlString, "", "POST", "/api/v1/auth/login", map[string]any{"email": f.ids["owner"] + "@fixture.invalid", "password": cipher(password)})
	if status != 200 || result["code"] != float64(0) || headers.Get("Authorization") == "" || strings.Contains(string(raw), hash) || result["data"].(map[string]any)["password"] != nil {
		t.Fatal("original login/profile", status, string(raw))
	}
	f.tokens["owner"] = headers.Get("Authorization")
	var token string
	var login time.Time
	var stamp int64
	var date time.Time
	if err := f.Read.QueryRow("SELECT access_token,last_login_time,update_time,update_date FROM user WHERE id=?", f.ids["owner"]).Scan(&token, &login, &stamp, &date); err != nil || snapshot("owner") == old || login.Before(before.Truncate(time.Second)) || login.Nanosecond() != 0 || stamp < before.UnixMilli() || date.Unix() != stamp/1000 {
		t.Fatal("login timestamp/token independent readback", login, stamp, date, err)
	}
	for _, actor := range []string{"member", "invite", "disabled"} {
		for _, path := range []string{"/api/v1/auth/login", "/api/v1/users/login"} {
			old := snapshot(actor)
			body := map[string]any{"email": f.ids[actor] + "@fixture.invalid", "username": f.ids[actor] + "@fixture.invalid", "password": cipher(password)}
			_, failed, _, headers := f.request(t, urlString, "", "POST", path, body)
			if failed["code"] == float64(0) || headers.Get("Authorization") != "" || snapshot(actor) != old {
				t.Fatal("disabled login succeeded or mutated SQL", actor, path, failed)
			}
		}
	}
	old = snapshot("owner")
	_, failed, _, headers := f.request(t, urlString, "", "POST", "/api/v1/auth/login", map[string]any{"email": f.ids["owner"] + "@fixture.invalid", "password": cipher("wrong")})
	if failed["code"] == float64(0) || headers.Get("Authorization") != "" || snapshot("owner") != old {
		t.Fatal("bad password changed SQL", failed)
	}
	originalGate := server.GetConfig().Authentication.DisablePasswordLogin
	server.GetConfig().Authentication.DisablePasswordLogin = true
	t.Cleanup(func() { server.GetConfig().Authentication.DisablePasswordLogin = originalGate })
	_, failed, _, headers = f.request(t, urlString, "", "POST", "/api/v1/auth/login", map[string]any{"email": f.ids["owner"] + "@fixture.invalid", "password": cipher(password)})
	if failed["code"] == float64(0) || headers.Get("Authorization") != "" || snapshot("owner") != old {
		t.Fatal("disabled password login changed SQL", failed)
	}
	server.GetConfig().Authentication.DisablePasswordLogin = false
	address, err := url.Parse(urlString)
	if err != nil {
		t.Fatal(err)
	}
	client := cli.NewHTTPClient()
	client.Host = address.Hostname()
	client.Port, _ = strconv.Atoi(address.Port())
	consumer := &cli.CLI{Config: &cli.CommandLineConfig{CLIMode: cli.APIMode, APIClientConfig: cli.APIModeConfig{CurrentAPIServer: "fixture", APIServerMap: map[string]*cli.APIServerConfig{"fixture": {Name: "fixture"}}}}, APIServerClientMap: map[string]*cli.HTTPClient{"fixture": client}}
	if err := consumer.LoginUserInteractive(f.ids["owner"]+"@fixture.invalid", password); err != nil {
		t.Fatal("actual CLI login", err)
	}
	if client.LoginToken == nil || *client.LoginToken == "" {
		t.Fatal("CLI did not retain returned token")
	}
	f.tokens["owner"] = *client.LoginToken
	_, profile, _, _ := f.request(t, urlString, "owner", "GET", "/api/v1/users/me", nil)
	if profile["code"] != float64(0) || profile["data"].(map[string]any)["password"] != nil {
		t.Fatal("profile credential leak", profile)
	}
	_, updated, _, _ := f.request(t, urlString, "owner", "PATCH", "/api/v1/users/me", map[string]any{"nickname": "", "language": "", "avatar": "", "id": f.ids["foreign"]})
	if updated["code"] != float64(0) {
		t.Fatal("explicit empty settings", updated)
	}
	var nickname, language, avatar string
	if err := f.Read.QueryRow("SELECT nickname,language,avatar FROM user WHERE id=?", f.ids["owner"]).Scan(&nickname, &language, &avatar); err != nil || nickname != "" || language != "" || avatar != "" {
		t.Fatal("empty settings/presence lost", nickname, language, avatar, err)
	}
	f.Journal.Event("canonical-user-login-settings-readback", map[string]any{"actual_rsa_cli_http_mysql": true, "inactive_status_disabled_password_zero_token_mutation": true, "password_hash_omitted": true, "epoch_ms_second_date_login": true, "empty_settings_identity_bound": true})
}
