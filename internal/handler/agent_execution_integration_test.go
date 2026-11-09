//go:build integration

package handler_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/dao"
	engineredis "multirag/internal/engine/redis"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/testsupport"
	"multirag/internal/testsupport/scratch"
	"multirag/internal/utility"
)

func TestAgentExecutionHTTPMySQL(t *testing.T) {
	f := scratch.New(t)
	if err := f.DB.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.APIToken{}, &entity.UserCanvas{}, &entity.UserCanvasVersion{}, &entity.API4Conversation{}); err != nil {
		t.Fatal(err)
	}
	previousDB := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = previousDB })
	secret := f.Prefix + "-signed-fixture"
	cfg := server.GetConfig()
	if cfg == nil {
		path := filepath.Join(t.TempDir(), "agent-auth.json")
		f.Journal.Event("reserve-local-config", map[string]any{"path": path})
		encoded, _ := json.Marshal(map[string]any{"server": map[string]any{"secret_key": secret}})
		if err := os.WriteFile(path, encoded, 0600); err != nil {
			t.Fatal(err)
		}
		if err := server.FromConfigFile(path); err != nil {
			t.Fatal(err)
		}
		cfg = server.GetConfig()
	}
	previousSecret := cfg.Server.SecretKey
	cfg.Server.SecretKey = &secret
	t.Cleanup(func() { cfg.Server.SecretKey = previousSecret })
	previousLicense := local.GetAdminStatus()
	local.SetAdminStatus(0, "isolated Agent acceptance")
	t.Cleanup(func() { local.SetAdminStatus(previousLicense.Status, previousLicense.Reason) })
	owner, foreign, disabled := f.Prefix+"owner", f.Prefix+"foreign", f.Prefix+"disabled"
	team, invited, revoked := f.Prefix+"team", f.Prefix+"invited", f.Prefix+"revoked"
	tokens := map[string]string{}
	for _, actor := range []string{owner, foreign, disabled, team, invited, revoked} {
		access := utility.GenerateToken()
		active := "1"
		if actor == disabled {
			active = "0"
		}
		user := &entity.User{ID: actor, Nickname: actor, Email: actor + "@fixture.invalid", AccessToken: &access, IsActive: active}
		if err := f.DB.Create(user).Error; err != nil {
			t.Fatal(err)
		}
		token, err := utility.DumpAccessToken(access, secret)
		if err != nil {
			t.Fatal(err)
		}
		tokens[actor] = token
	}
	for _, member := range []struct{ id, role, status string }{{team, "normal", "1"}, {invited, "invite", "1"}, {revoked, "normal", "0"}} {
		status := member.status
		if err := f.DB.Create(&entity.UserTenant{ID: utility.GenerateToken(), UserID: member.id, TenantID: owner, Role: member.role, Status: &status}).Error; err != nil {
			t.Fatal(err)
		}
	}

	apiToken := utility.GenerateAPIToken()
	if err := f.DB.Create(&entity.APIToken{TenantID: owner, Token: apiToken}).Error; err != nil {
		t.Fatal(err)
	}
	gin.SetMode(gin.TestMode)
	app := gin.New()
	app.Use(gin.Recovery())
	router.RegisterAgentRoutes(app.Group("/api/v1/agents", handler.NewAuthHandler().AuthMiddleware()), handler.NewAgentHandler(service.NewAgentService(), nil))
	httpServer := testsupport.NewHTTP(t, app)
	request := func(actor, method, path, body string) map[string]any {
		t.Helper()
		req, err := http.NewRequest(method, httpServer.URL+"/api/v1/agents"+path, strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		if actor != "" {
			credential := tokens[actor]
			if actor == "api" {
				credential = apiToken
			}
			req.Header.Set("Authorization", "Bearer "+credential)
		}
		req.Header.Set("Content-Type", "application/json")
		response, err := httpServer.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		raw, err := io.ReadAll(response.Body)
		if err != nil {
			t.Fatal(err)
		}
		if bytes.Contains(raw, []byte("[DONE]")) || strings.Contains(response.Header.Get("Content-Type"), "event-stream") {
			t.Fatal("synthetic SSE")
		}
		var result map[string]any
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.UseNumber()
		if err := decoder.Decode(&result); err != nil {
			t.Fatalf("invalid HTTP JSON: %d %s", response.StatusCode, raw)
		}
		return result
	}
	success := func(result map[string]any) map[string]any {
		t.Helper()
		if result["code"] != json.Number("0") {
			t.Fatal("HTTP operation failed", result)
		}
		data, _ := result["data"].(map[string]any)
		return data
	}
	denied := func(result map[string]any) {
		t.Helper()
		if result["code"] == json.Number("0") {
			t.Fatal("unexpected HTTP success", result)
		}
	}
	denied(request("", "POST", "", `{"title":"unowned","dsl":{}}`))
	if result := request(disabled, "POST", "", `{"title":"disabled","dsl":{}}`); result["code"] == json.Number("0") {
		t.Error("disabled actor created Agent")
	}
	created := success(request(owner, "POST", "", `{"title":"target","canvas_category":"agent_canvas","user_id":"forged","dsl":{"version":1,"exact":9007199254740993,"components":{}}}`))
	id := created["id"].(string)
	var storedOwner, category, dsl string
	if err := f.Read.QueryRow("SELECT user_id, canvas_category, dsl FROM user_canvas WHERE id = ?", id).Scan(&storedOwner, &category, &dsl); err != nil || storedOwner != owner || category != "agent_canvas" || !strings.Contains(dsl, "9007199254740993") {
		t.Fatalf("create readback mismatch: %s %s %s %v", storedOwner, category, dsl, err)
	}
	denied(request(owner, "POST", "", `{"title":"target","dsl":{}}`))
	denied(request(foreign, "GET", "/"+id, ""))
	denied(request(foreign, "PUT", "/"+id, `{"dsl":{"forged":true}}`))
	success(request("api", "GET", "/"+id, ""))
	listing := success(request(owner, "GET", "", ""))
	if listing["total"] != json.Number("1") {
		t.Fatal("actual list mismatch", listing)
	}
	denied(request(owner, "GET", "?orderby=id%20DESC%3BSELECT%201", ""))
	published := success(request(owner, "POST", "/"+id+"/publish", `{"title":"released","description":"exact target","dsl":{"version":2,"exact":9007199254740993,"snapshot":"frozen"}}`))
	version := published["id"].(string)
	var title, description string
	var release bool
	if err := f.Read.QueryRow("SELECT title,description,`release`,dsl FROM user_canvas WHERE id = ?", id).Scan(&title, &description, &release, &dsl); err != nil || title != "released" || description != "exact target" || !release || !strings.Contains(dsl, "9007199254740993") {
		t.Fatal("publish override readback", err, title, description, release, dsl)
	}
	success(request(owner, "PUT", "/"+id, `{"dsl":{"version":2,"snapshot":"draft"}}`))
	snapshot := success(request(owner, "GET", "/"+id+"/versions/"+version, ""))
	if snapshot["dsl"].(map[string]any)["snapshot"] != "frozen" || snapshot["dsl"].(map[string]any)["exact"] != json.Number("9007199254740993") {
		t.Fatal("immutable snapshot/precision changed", snapshot)
	}
	other := success(request(owner, "POST", "", `{"title":"other","dsl":{}}`))["id"].(string)
	denied(request(owner, "GET", "/"+other+"/versions/"+version, ""))
	denied(request(owner, "DELETE", "/"+other+"/versions/"+version, ""))
	shared := success(request(owner, "POST", "", `{"title":"shared target","permission":"team","dsl":{}}`))["id"].(string)
	success(request(team, "GET", "/"+shared, ""))
	denied(request(team, "GET", "/"+id, ""))
	denied(request(invited, "GET", "/"+shared, ""))
	denied(request(revoked, "GET", "/"+shared, ""))
	denied(request(team, "DELETE", "/"+shared, ""))
	denied(request(invited, "GET", "?owner_ids="+owner, ""))

	session := success(request(owner, "POST", "/"+id+"/sessions", `{"name":"real session","dsl":{"version":2,"exact":9007199254740993}}`))
	sessionID := session["id"].(string)
	var dialog, sessionOwner, message string
	if err := f.Read.QueryRow("SELECT dialog_id,user_id,message,dsl FROM api_4_conversation WHERE id = ?", sessionID).Scan(&dialog, &sessionOwner, &message, &dsl); err != nil || dialog != id || sessionOwner != owner || message != "[]" || !strings.Contains(dsl, "9007199254740993") {
		t.Fatal("session persistence readback", err, dialog, sessionOwner, message, dsl)
	}
	denied(request(foreign, "GET", "/"+id+"/sessions/"+sessionID, ""))
	denied(request(foreign, "DELETE", "/"+id+"/sessions/"+sessionID, ""))
	list := request(owner, "GET", "/"+id+"/sessions", "")
	if list["code"] != json.Number("0") {
		t.Fatal(list)
	}
	for _, raw := range list["data"].([]any) {
		if _, exists := raw.(map[string]any)["dsl"]; exists {
			t.Fatal("default DSL query changed")
		}
	}
	withDSL := request(owner, "GET", "/"+id+"/sessions?dsl=true", "")
	if withDSL["code"] != json.Number("0") || withDSL["data"].([]any)[0].(map[string]any)["dsl"] == nil {
		t.Fatal("explicit DSL query ignored", withDSL)
	}
	var before int
	if err := f.Read.QueryRow("SELECT count(*) FROM api_4_conversation").Scan(&before); err != nil {
		t.Fatal(err)
	}
	for _, action := range []struct{ method, path, body string }{{"POST", "/" + id + "/run", ""}, {"POST", "/" + id + "/run?version=" + version, ""}, {"POST", "/chat/completions", fmt.Sprintf(`{"agent_id":%q,"query":"hello","stream":true}`, id)}, {"POST", "/rerun", fmt.Sprintf(`{"id":%q,"dsl":{},"component_id":"begin"}`, id)}, {"GET", "/" + id + "/webhook/logs", ""}, {"GET", "/tags", ""}} {
		result := request(owner, action.method, action.path, action.body)
		denied(result)
		if !strings.Contains(fmt.Sprint(result["message"]), "not implemented") {
			t.Fatal("unsupported capability not explicit", result)
		}
	}
	var after int
	if err := f.Read.QueryRow("SELECT count(*) FROM api_4_conversation").Scan(&after); err != nil || after != before {
		t.Fatal("unsupported execution wrote sessions")
	}
	if len(f.ObjectKeys(t)) != 0 {
		t.Fatal("unsupported execution wrote objects")
	}
	if err := f.DB.Create(&entity.APIToken{TenantID: foreign, Token: apiToken}).Error; err != nil {
		t.Fatal(err)
	}
	denied(request("api", "GET", "/prompts", ""))
	if err := f.DB.Where("tenant_id = ? AND token = ?", foreign, apiToken).Delete(&entity.APIToken{}).Error; err != nil {
		t.Fatal(err)
	}
	success(request("api", "GET", "/prompts", ""))

	f.Journal.Event("reserve-sql-trigger", map[string]any{"database": f.Database, "trigger": "agent_block_publish"})
	if err := f.DB.Exec("CREATE TRIGGER agent_block_publish BEFORE UPDATE ON user_canvas FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'controlled publish failure'").Error; err != nil {
		t.Fatal(err)
	}
	denied(request(owner, "POST", "/"+id+"/publish", `{}`))
	if err := f.DB.Exec("DROP TRIGGER agent_block_publish").Error; err != nil {
		t.Fatal(err)
	}
	var versions int
	if err := f.Read.QueryRow("SELECT count(*) FROM user_canvas_version WHERE user_canvas_id = ?", id).Scan(&versions); err != nil || versions != 1 {
		t.Fatal("failed publish left snapshot", err, versions)
	}
	success(request(owner, "DELETE", "/"+id+"/sessions/"+sessionID, ""))
	denied(request(foreign, "DELETE", "/"+id, ""))
	success(request(owner, "DELETE", "/"+id, ""))
	if err := f.Read.QueryRow("SELECT count(*) FROM user_canvas_version WHERE user_canvas_id = ?", id).Scan(&versions); err != nil || versions != 0 {
		t.Fatal("delete left snapshots", err, versions)
	}
	f.Journal.Event("agent-http-sql-acceptance", map[string]any{"canonical_target": "fcebcebe1e0e798b42c18e5593747cd20eb4c757", "http": true, "independent_sql_readback": true, "real_provider": false, "execution_supported": false})
}

func TestAgentLogsRedisHTTP(t *testing.T) {
	f := scratch.New(t)
	if err := f.DB.AutoMigrate(&entity.User{}, &entity.APIToken{}, &entity.UserTenant{}, &entity.UserCanvas{}); err != nil {
		t.Fatal(err)
	}
	previousDB := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = previousDB })
	cfg := server.GetConfig()
	if cfg == nil {
		path := filepath.Join(t.TempDir(), "agent-log-auth.json")
		f.Journal.Event("reserve-local-config", map[string]any{"path": path})
		if err := os.WriteFile(path, []byte(`{"server":{"secret_key":"isolated-agent-log-config"}}`), 0600); err != nil {
			t.Fatal(err)
		}
		if err := server.FromConfigFile(path); err != nil {
			t.Fatal(err)
		}
		cfg = server.GetConfig()
	}
	previousSecret := cfg.Server.SecretKey
	secret := f.Prefix + "logs"
	cfg.Server.SecretKey = &secret
	t.Cleanup(func() { cfg.Server.SecretKey = previousSecret })
	priorLicense := local.GetAdminStatus()
	local.SetAdminStatus(0, "isolated log acceptance")
	t.Cleanup(func() { local.SetAdminStatus(priorLicense.Status, priorLicense.Reason) })
	tokens := map[string]string{}
	for _, actor := range []string{"owner", "foreign"} {
		access := utility.GenerateToken()
		id := f.Prefix + actor
		if err := f.DB.Create(&entity.User{ID: id, Nickname: id, Email: id + "@fixture.invalid", AccessToken: &access}).Error; err != nil {
			t.Fatal(err)
		}
		token, err := utility.DumpAccessToken(access, secret)
		if err != nil {
			t.Fatal(err)
		}
		tokens[actor] = token
	}
	canvasID := f.Prefix + "canvas"
	if err := f.DB.Create(&entity.UserCanvas{ID: canvasID, UserID: f.Prefix + "owner"}).Error; err != nil {
		t.Fatal(err)
	}
	f.Journal.Event("reserve-redis-client", map[string]any{"redis_db": f.Config.Redis.DB, "purpose": "Agent log reader"})
	if engineredis.Get() != nil {
		t.Fatal("Agent Redis acceptance requires a fresh process")
	}
	if err := engineredis.Init(&f.Config.Redis); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = engineredis.Close() })
	app := gin.New()
	router.RegisterAgentRoutes(app.Group("/api/v1/agents", handler.NewAuthHandler().AuthMiddleware()), handler.NewAgentHandler(service.NewAgentService(), nil))
	httpServer := testsupport.NewHTTP(t, app)
	messageID := f.Prefix + "trace"
	key := canvasID + "-" + messageID + "-logs"
	f.Journal.Event("reserve-redis-key", map[string]any{"key": key, "redis_db": f.Config.Redis.DB})
	if count, err := f.Redis.Exists(t.Context(), key).Result(); err != nil || count != 0 {
		t.Fatal("log key precondition")
	}
	fetch := func(actor string) map[string]any {
		t.Helper()
		req, _ := http.NewRequest("GET", httpServer.URL+"/api/v1/agents/"+canvasID+"/logs/"+messageID, nil)
		req.Header.Set("Authorization", "Bearer "+tokens[actor])
		response, err := httpServer.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		var result map[string]any
		if err := json.NewDecoder(response.Body).Decode(&result); err != nil {
			t.Fatal(err)
		}
		return result
	}
	if err := f.Redis.Set(t.Context(), key, `{"node":"actual persisted logs"}`, 0).Err(); err != nil {
		t.Fatal(err)
	}
	result := fetch("owner")
	if result["code"] != float64(0) || result["data"].(map[string]any)["node"] != "actual persisted logs" {
		t.Fatal("real logs not returned", result)
	}
	result = fetch("foreign")
	if result["code"] == float64(0) || strings.Contains(fmt.Sprint(result), "actual persisted logs") {
		t.Fatal("foreign logs disclosed", result)
	}
	if err := f.Redis.Set(t.Context(), key, `broken`, 0).Err(); err != nil {
		t.Fatal(err)
	}
	if result = fetch("owner"); result["code"] == float64(0) {
		t.Fatal("corrupt logs became empty success", result)
	}
	if err := f.Redis.Del(t.Context(), key).Err(); err != nil {
		t.Fatal(err)
	}
	if result = fetch("owner"); result["code"] != float64(0) || len(result["data"].(map[string]any)) != 0 {
		t.Fatal("missing logs contract", result)
	}
	if err := engineredis.Get().GetClient().Close(); err != nil {
		t.Fatal(err)
	}
	if result = fetch("owner"); result["code"] == float64(0) {
		t.Fatal("failed logs backend became empty success", result)
	}
}
