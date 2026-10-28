package router_test

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"gorm.io/gorm"
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

// TestAfterFFModelsHTTPAndSQL exercises the registered router and actual CLI
// consumer against an exclusively owned PostgreSQL database. Each write target
// is reserved before creation; a separate SQL connection performs the readback.
func TestAfterFFModelsHTTPAndSQL(t *testing.T) {
	journal := testsupport.NewCase(t)
	config, address := os.Getenv("MULTIRAG_AFTERFF_G_CONFIG"), os.Getenv("MULTIRAG_AFTERFF_G_ADDRESS")
	if config == "" || address == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned SQL config and exact listener required")
		}
		t.Skip("requires owned afterff G SQL config and listener")
	}
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal("owned config could not be loaded")
	}
	if len(server.GetConfig().Server.SecretKey) < 32 {
		t.Fatal("owned fixture requires a configured signing key")
	}
	// The configured key initializes process-local runtime variables without
	// touching the shared Redis system secret-key namespace.
	if err := server.InitVariables(nil); err != nil {
		t.Fatal("fixture runtime signing variables unavailable")
	}
	cfg := server.GetConfig()
	if cfg.Database.Host != "127.0.0.1" || cfg.Database.Port != 5432 || cfg.Database.Database != "multirag_batch_g_afterff_20261028" || cfg.Database.Schema != "usr_ai" {
		t.Fatal("exact owned PostgreSQL target required")
	}
	journal.Event("reserve-schema-before-SQL", map[string]any{"database": cfg.Database.Database, "schema": cfg.Database.Schema, "host": cfg.Database.Host, "port": cfg.Database.Port})
	if err := dao.InitDB(); err != nil {
		t.Fatal("owned schema initialization failed")
	}
	pool, err := dao.DB.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = pool.Close() })
	dsn, err := dao.PostgresDSN(cfg.Database)
	if err != nil {
		t.Fatal(err)
	}
	read, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("independent SQL connection failed")
	}
	t.Cleanup(func() { _ = read.Close() })
	if err := read.PingContext(context.Background()); err != nil {
		t.Fatal("independent SQL connection unavailable")
	}

	suffix := os.Getenv("MULTIRAG_RUN_ID")
	if len(suffix) < 12 {
		suffix = strings.ReplaceAll(uuid.NewString(), "-", "")
	}
	suffix = suffix[:12]
	owner, other, member, inactive := "gff-own-"+suffix, "gff-oth-"+suffix, "gff-mem-"+suffix, "gff-off-"+suffix
	providerID, otherProviderID, instanceID, otherInstanceID := "gff-p-"+suffix, "gff-op-"+suffix, "gff-i-"+suffix, "gff-oi-"+suffix
	ownerMembership, otherMembership, memberMembership := "gff-om-"+suffix, "gff-otm-"+suffix, "gff-mm-"+suffix
	instanceName := "instance-" + suffix
	callbackName := "afterff_models_resources_" + suffix
	if err := dao.DB.Callback().Create().Before("gorm:create").Register(callbackName, func(tx *gorm.DB) {
		value := tx.Statement.ReflectValue
		if value.Kind() == reflect.Pointer {
			value = value.Elem()
		}
		rows := []any{}
		appendID := func(v reflect.Value) {
			if v.Kind() == reflect.Pointer {
				v = v.Elem()
			}
			if v.Kind() == reflect.Struct {
				field := v.FieldByName("ID")
				if field.IsValid() {
					rows = append(rows, field.Interface())
				}
			}
		}
		if value.Kind() == reflect.Slice || value.Kind() == reflect.Array {
			for i := 0; i < value.Len(); i++ {
				appendID(value.Index(i))
			}
		} else {
			appendID(value)
		}
		journal.Event("reserve-rows-before-SQL", map[string]any{"table": tx.Statement.Table, "ids": rows})
	}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = dao.DB.Callback().Create().Remove(callbackName) })
	mustCreate := func(value any) {
		t.Helper()
		if err := dao.DB.Create(value).Error; err != nil {
			state := "unavailable"
			var sqlState interface{ SQLState() string }
			if errors.As(err, &sqlState) {
				state = sqlState.SQLState()
			}
			t.Fatalf("owned row creation failed: row_type=%T error_type=%T SQLSTATE=%s", value, err, state)
		}
	}
	active, disabled := "1", "0"
	for _, id := range []string{owner, other, member, inactive} {
		status := active
		if id == inactive {
			status = disabled
		}
		mustCreate(&entity.User{ID: id, Email: id + "@example.invalid", Nickname: id, Status: &status, IsActive: true, IsAuthenticated: true})
		mustCreate(&entity.Tenant{ID: id, Status: &active})
	}
	mustCreate(&entity.UserTenant{ID: ownerMembership, UserID: owner, TenantID: owner, InvitedBy: owner, Role: "owner", Status: &active})
	mustCreate(&entity.UserTenant{ID: otherMembership, UserID: other, TenantID: other, InvitedBy: other, Role: "owner", Status: &active})
	mustCreate(&entity.UserTenant{ID: memberMembership, UserID: member, TenantID: owner, InvitedBy: owner, Role: "normal", Status: &active})
	mustCreate(&entity.TenantModelProvider{ID: providerID, ProviderName: "vllm", TenantID: owner})
	mustCreate(&entity.TenantModelProvider{ID: otherProviderID, ProviderName: "vllm", TenantID: other})
	instanceExtra := `{"region":"default","base_url":"http://127.0.0.1:9/v1"}`
	mustCreate(&entity.TenantModelInstance{ID: instanceID, ProviderID: providerID, InstanceName: instanceName, Status: "active", APIKey: "", Extra: instanceExtra})
	mustCreate(&entity.TenantModelInstance{ID: otherInstanceID, ProviderID: otherProviderID, InstanceName: "other-instance", Status: "active", APIKey: "", Extra: `{"region":"default"}`})

	// Cleanup remains exact even when a later assertion fails. Database ownership
	// is also recorded by the outer fixture, which drops the whole scratch DB.
	t.Cleanup(func() {
		for _, statement := range []struct {
			query string
			args  []any
		}{
			{"DELETE FROM tenant_model WHERE provider_id IN ($1,$2)", []any{providerID, otherProviderID}},
			{"DELETE FROM tenant_model_instance WHERE provider_id IN ($1,$2)", []any{providerID, otherProviderID}},
			{"DELETE FROM tenant_model_provider WHERE id IN ($1,$2)", []any{providerID, otherProviderID}},
			{"DELETE FROM t_ai_user_tenants WHERE id IN ($1,$2,$3)", []any{ownerMembership, otherMembership, memberMembership}},
			{"DELETE FROM t_ai_users WHERE id IN ($1,$2,$3,$4)", []any{owner, other, member, inactive}},
			{"DELETE FROM t_ai_tenants WHERE id IN ($1,$2,$3,$4)", []any{owner, other, member, inactive}},
		} {
			if _, err := read.Exec(statement.query, statement.args...); err != nil {
				t.Error("owned row cleanup failed")
			}
		}
		var remaining int
		err := read.QueryRow("SELECT (SELECT count(*) FROM tenant_model WHERE provider_id IN ($1,$2))+(SELECT count(*) FROM tenant_model_instance WHERE provider_id IN ($1,$2))+(SELECT count(*) FROM tenant_model_provider WHERE id IN ($1,$2))", providerID, otherProviderID).Scan(&remaining)
		journal.Event("cleanup-model-SQL-readback", map[string]any{"provider_ids": []string{providerID, otherProviderID}, "remaining": remaining, "confirmed": err == nil && remaining == 0})
		if err != nil || remaining != 0 {
			t.Error("owned model SQL rows remain")
		}
		var identityRemaining int
		err = read.QueryRow("SELECT (SELECT count(*) FROM t_ai_users WHERE id IN ($1,$2,$3,$4))+(SELECT count(*) FROM t_ai_tenants WHERE id IN ($1,$2,$3,$4))+(SELECT count(*) FROM t_ai_user_tenants WHERE user_id IN ($1,$2,$3,$4))", owner, other, member, inactive).Scan(&identityRemaining)
		journal.Event("cleanup-identity-SQL-readback", map[string]any{"user_ids": []string{owner, other, member, inactive}, "remaining": identityRemaining, "confirmed": err == nil && identityRemaining == 0})
		if err != nil || identityRemaining != 0 {
			t.Error("owned identity SQL rows remain")
		}
	})

	snapshot := func() string {
		t.Helper()
		var parts []string
		for _, table := range []string{"tenant_model", "tenant_model_instance", "tenant_model_provider", "t_ai_users", "t_ai_tenants", "t_ai_user_tenants", "t_ai_connectors"} {
			var rows string
			if err := read.QueryRow("SELECT COALESCE(jsonb_agg(to_jsonb(m) ORDER BY to_jsonb(m)::text)::text,'[]') FROM " + table + " m").Scan(&rows); err != nil {
				t.Fatal("independent SQL snapshot failed")
			}
			parts = append(parts, rows)
		}
		return fmt.Sprintf("%x", sha256.Sum256([]byte(strings.Join(parts, "\n"))))
	}
	beforeAdmin := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned afterff fixture")
	t.Cleanup(func() { local.SetAdminStatus(beforeAdmin.Status, beforeAdmin.Reason) })
	app := gin.New()
	svc := service.NewModelProviderService()
	router.NewRouter(handler.NewAuthHandler(), nil, nil, nil, nil, handler.NewSystemHandler(service.NewSystemService()), nil, nil, nil, nil, handler.NewConnectorHandler(service.NewConnectorService(), service.NewUserService()), nil, nil, nil, handler.NewProviderHandler(nil, svc), nil).Setup(app)
	foundNew := false
	for _, route := range app.Routes() {
		for _, retired := range []string{"/v1/llm/my_llms", "/v1/llm/factories", "/v1/llm/list", "/v1/llm/set_api_key"} {
			if route.Path == retired {
				t.Fatalf("retired route still registered: %s", retired)
			}
		}
		if route.Method == http.MethodPost && route.Path == "/api/v1/providers/:provider_name/instances/:instance_name/models" {
			foundNew = true
		}
	}
	if !foundNew {
		t.Fatal("active provider declaration route missing")
	}
	journal.Event("reserve-listener", map[string]any{"address": address})
	listener, err := net.Listen("tcp", address)
	if err != nil {
		t.Fatal("exact fixture listener unavailable")
	}
	live := &httptest.Server{Listener: listener, Config: &http.Server{Handler: app}}
	live.Start()
	t.Cleanup(func() {
		live.Close()
		connection, err := net.DialTimeout("tcp", address, 150*time.Millisecond)
		if connection != nil {
			_ = connection.Close()
		}
		journal.Event("cleanup-listener-readback", map[string]any{"address": address, "absent": err != nil})
		if err == nil {
			t.Error("owned HTTP listener remains")
		}
	})
	transport := http.DefaultTransport.(*http.Transport).Clone()
	t.Cleanup(transport.CloseIdleConnections)
	client := &http.Client{Transport: transport, Timeout: 8 * time.Second}
	tokens := map[string]string{}
	for _, id := range []string{owner, other, member, inactive} {
		token, err := utility.SignUserJWT(id+"@example.invalid", server.GetSecretKey())
		if err != nil {
			t.Fatal("fixture JWT signing failed")
		}
		tokens[id] = token
	}
	type outcome struct {
		status int
		code   common.ErrorCode
		raw    string
		err    error
	}
	request := func(actor, method, path, body string) outcome {
		req, err := http.NewRequest(method, live.URL+path, strings.NewReader(body))
		if err != nil {
			return outcome{err: err}
		}
		req.Header.Set("Content-Type", "application/json")
		if actor == "invalid-token" {
			req.Header.Set("Authorization", "Bearer invalid-token")
		} else if actor != "" {
			req.Header.Set("Authorization", "Bearer "+tokens[actor])
		}
		response, err := client.Do(req)
		if err != nil {
			return outcome{err: err}
		}
		raw, readErr := io.ReadAll(io.LimitReader(response.Body, 1<<20))
		_ = response.Body.Close()
		if readErr != nil {
			return outcome{err: readErr}
		}
		var envelope struct {
			Code *common.ErrorCode `json:"code"`
		}
		if err := json.Unmarshal(raw, &envelope); err != nil || envelope.Code == nil {
			return outcome{status: response.StatusCode, err: fmt.Errorf("invalid result envelope")}
		}
		return outcome{status: response.StatusCode, code: *envelope.Code, raw: string(raw)}
	}
	path := "/api/v1/providers/vllm/instances/" + instanceName + "/models"
	check := func(actor, route, body string, want common.ErrorCode) {
		t.Helper()
		got := request(actor, http.MethodPost, route, body)
		if got.err != nil || got.status != http.StatusOK || got.code != want {
			t.Fatalf("model declaration status=%d code=%d want=%d err=%v", got.status, got.code, want, got.err)
		}
		if strings.Contains(got.raw, "private-trigger-detail") {
			t.Fatal("internal SQL error leaked")
		}
	}
	for _, actor := range []string{"", "invalid-token", inactive} {
		before := snapshot()
		got := request(actor, http.MethodPost, path, `{"models":[{"model_name":"blocked","model_type":"chat","max_tokens":0}]}`)
		if got.err != nil || got.status != http.StatusUnauthorized || snapshot() != before {
			t.Fatal("unauthorized declaration changed SQL")
		}
	}
	for _, oauthPath := range []string{"/api/v1/connectors/google/oauth/web/start?source=google-drive", "/api/v1/connectors/google/oauth/web/result?source=google-drive"} {
		for _, actor := range []string{"", "invalid-token", inactive} {
			before := snapshot()
			got := request(actor, http.MethodPost, oauthPath, `{invalid`)
			if got.err != nil || got.status != http.StatusUnauthorized || snapshot() != before {
				t.Fatal("OAuth authentication guard failed")
			}
		}
		before := snapshot()
		got := request(owner, http.MethodPost, oauthPath, `{invalid`)
		if got.err != nil || got.status != http.StatusOK || got.code != common.CodeBadRequest || snapshot() != before {
			t.Fatal("authenticated malformed OAuth request reached service")
		}
	}
	for _, body := range []string{
		`{}`, `null`, `{"models":null}`, `{"models":[]}`, `{"models":{}}`,
		`{"models":[{"model_name":"invalid","model_type":"chat","max_tokens":0}],"model_name":"mixed"}`,
		`{"models":[{"model_name":"invalid","model_type":"chat"}]}`,
		`{"models":[{"model_name":"invalid","model_type":"chat","max_tokens":null}]}`,
		`{"models":[{"model_name":"invalid","model_type":"chat","max_tokens":-1}]}`,
		`{"models":[{"model_name":"invalid","model_type":"chat","max_tokens":false}]}`,
		`{"models":[{"model_name":"invalid","model_type":"chat","max_tokens":1.5}]}`,
		`{"models":[{"model_name":"invalid","model_types":[],"max_tokens":0}]}`,
		`{"models":[{"model_name":"invalid","model_types":["chat","chat"],"max_tokens":0}]}`,
		`{"models":[{"model_name":"invalid","model_type":"embedding","model_types":["chat","embedding"],"max_tokens":0}]}`,
		`{"models":[{"model_name":"invalid","model_type":"unknown","max_tokens":0}]}`,
		`{"models":[{"model_name":"invalid","model_type":"embedding","max_tokens":0,"thinking":false}]}`,
		`{"models":[{"provider_name":"other","model_name":"invalid","model_type":"chat","max_tokens":0}]}`,
		`{"models":[{"model_name":"duplicate","model_type":"chat","max_tokens":0},{"model_name":"duplicate","model_type":"chat","max_tokens":0}]}`,
	} {
		before := snapshot()
		check(owner, path, body, common.CodeBadRequest)
		if snapshot() != before {
			t.Fatal("invalid batch changed SQL")
		}
	}
	for _, actor := range []string{other, member} {
		before := snapshot()
		check(actor, path, `{"models":[{"model_name":"denied","model_type":"chat","max_tokens":0}]}`, common.CodeNotFound)
		if snapshot() != before {
			t.Fatal("tenant or membership denial changed SQL")
		}
	}
	if err := dao.DB.Model(&entity.TenantModelInstance{}).Where("id = ?", instanceID).Update("status", "inactive").Error; err != nil {
		t.Fatal(err)
	}
	before := snapshot()
	check(owner, path, `{"models":[{"model_name":"denied","model_type":"chat","max_tokens":0}]}`, common.CodeDataError)
	if snapshot() != before {
		t.Fatal("disabled instance declaration changed SQL")
	}
	if err := dao.DB.Model(&entity.TenantModelInstance{}).Where("id = ?", instanceID).Update("status", "active").Error; err != nil {
		t.Fatal(err)
	}

	check(owner, path, `{"models":[{"model_name":"multi","model_types":["chat","vision","embedding"],"max_tokens":0,"thinking":false},{"model_name":"second","model_type":"embedding","max_tokens":1024}]}`, common.CodeSuccess)
	var primary, raw string
	if err := read.QueryRow("SELECT model_type,extra FROM tenant_model WHERE instance_id=$1 AND model_name='multi'", instanceID).Scan(&primary, &raw); err != nil {
		t.Fatal("model SQL readback failed")
	}
	var extra struct {
		MaxTokens  int      `json:"max_tokens"`
		ModelTypes []string `json:"model_types"`
		Thinking   *bool    `json:"thinking"`
	}
	if json.Unmarshal([]byte(raw), &extra) != nil || primary != "chat" || extra.MaxTokens != 0 || !reflect.DeepEqual(extra.ModelTypes, []string{"chat", "image2text", "embedding"}) || extra.Thinking == nil || *extra.Thinking {
		t.Fatal("batch lost zero, capability order, or explicit false")
	}
	before = snapshot()
	check(owner, path, `{"models":[{"model_name":"must-rollback","model_type":"chat","max_tokens":0},{"model_name":"multi","model_type":"chat","max_tokens":0}]}`, common.CodeConflict)
	if snapshot() != before {
		t.Fatal("conflicting batch partially committed")
	}

	functionName, triggerName := "gff_insert_failure_"+suffix, "gff_insert_trigger_"+suffix
	journal.Event("reserve-SQL-function-trigger", map[string]any{"function": functionName, "trigger": triggerName, "table": "tenant_model"})
	if _, err := read.Exec("CREATE FUNCTION " + functionName + "() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.instance_id='" + instanceID + "' AND NEW.model_name='z-fail' THEN RAISE EXCEPTION 'private-trigger-detail'; END IF; RETURN NEW; END $$"); err != nil {
		t.Fatal("owned failure function creation failed")
	}
	triggerCreated := false
	t.Cleanup(func() {
		if triggerCreated {
			if _, err := read.Exec("DROP TRIGGER IF EXISTS " + triggerName + " ON tenant_model"); err != nil {
				t.Error("owned trigger cleanup failed")
			}
		}
		if _, err := read.Exec("DROP FUNCTION IF EXISTS " + functionName + "()"); err != nil {
			t.Error("owned function cleanup failed")
		}
		var remaining int
		err := read.QueryRow("SELECT (SELECT count(*) FROM pg_proc WHERE proname=$1)+(SELECT count(*) FROM pg_trigger WHERE tgname=$2)", functionName, triggerName).Scan(&remaining)
		journal.Event("cleanup-SQL-function-trigger-readback", map[string]any{"function": functionName, "trigger": triggerName, "remaining": remaining, "confirmed": err == nil && remaining == 0})
		if err != nil || remaining != 0 {
			t.Error("owned failure injection remains")
		}
	})
	if _, err := read.Exec("CREATE TRIGGER " + triggerName + " BEFORE INSERT ON tenant_model FOR EACH ROW EXECUTE FUNCTION " + functionName + "()"); err != nil {
		t.Fatal("owned failure trigger creation failed")
	}
	triggerCreated = true
	before = snapshot()
	check(owner, path, `{"models":[{"model_name":"a-before-failure","model_type":"chat","max_tokens":0},{"model_name":"z-fail","model_type":"chat","max_tokens":0}]}`, common.CodeServerError)
	if snapshot() != before {
		t.Fatal("SQL batch failure partially committed")
	}
	if _, err := read.Exec("DROP TRIGGER " + triggerName + " ON tenant_model"); err != nil {
		t.Fatal("owned trigger removal failed")
	}
	triggerCreated = false

	results := make([]outcome, 2)
	var wait sync.WaitGroup
	for i := range results {
		wait.Add(1)
		go func(i int) {
			defer wait.Done()
			results[i] = request(owner, http.MethodPost, path, `{"models":[{"model_name":"concurrent-a","model_type":"chat","max_tokens":0},{"model_name":"concurrent-b","model_type":"chat","max_tokens":0}]}`)
		}(i)
	}
	wait.Wait()
	counts := map[common.ErrorCode]int{}
	for _, result := range results {
		if result.err != nil || result.status != http.StatusOK {
			t.Fatal("concurrent HTTP request failed")
		}
		counts[result.code]++
	}
	if counts[common.CodeSuccess] != 1 || counts[common.CodeConflict] != 1 {
		t.Fatal("concurrent duplicate batch was not serialized")
	}
	var stored int
	if err := read.QueryRow("SELECT count(*) FROM tenant_model WHERE instance_id=$1 AND model_name IN ('concurrent-a','concurrent-b')", instanceID).Scan(&stored); err != nil || stored != 2 {
		t.Fatal("concurrent batch duplicated or lost rows")
	}

	parsedHost, parsedPort, err := net.SplitHostPort(address)
	if err != nil {
		t.Fatal(err)
	}
	port, err := strconv.Atoi(parsedPort)
	if err != nil {
		t.Fatal(err)
	}
	cliHTTP := cli.NewHTTPClient()
	cliHTTP.Host, cliHTTP.Port, cliHTTP.LoginToken = parsedHost, port, tokens[owner]
	consumer := &cli.MultiRAGClient{HTTPClient: cliHTTP, ServerType: "user"}
	defaultInstance := "url-default-" + suffix
	createCommand, err := cli.NewParser("CREATE PROVIDER 'vllm' INSTANCE '" + defaultInstance + "' KEY '' URL 'http://127.0.0.1:9/v1';").Parse(false)
	if err != nil {
		t.Fatal("CLI URL-only instance parse failed")
	}
	if _, err := consumer.ExecuteCommand(createCommand); err != nil {
		t.Fatal("CLI URL-only instance creation failed")
	}
	var defaultExtra string
	if err := read.QueryRow("SELECT extra FROM tenant_model_instance WHERE provider_id=$1 AND instance_name=$2", providerID, defaultInstance).Scan(&defaultExtra); err != nil {
		t.Fatal("CLI instance readback failed")
	}
	var endpoint struct {
		Region  string `json:"region"`
		BaseURL string `json:"base_url"`
	}
	if json.Unmarshal([]byte(defaultExtra), &endpoint) != nil || endpoint.Region != "default" || endpoint.BaseURL != "http://127.0.0.1:9/v1" {
		t.Fatal("CLI instance name became a region or lost URL override")
	}
	duplicateInstance := "instance-race-" + suffix
	instanceResults := make([]outcome, 2)
	for i := range instanceResults {
		wait.Add(1)
		go func(i int) {
			defer wait.Done()
			instanceResults[i] = request(owner, http.MethodPost, "/api/v1/providers/vllm/instances", `{"instance_name":"`+duplicateInstance+`","api_key":"","base_url":"http://127.0.0.1:9/v1","region":"custom-region"}`)
		}(i)
	}
	wait.Wait()
	instanceCodes := map[common.ErrorCode]int{}
	for _, result := range instanceResults {
		if result.err != nil || result.status != http.StatusOK {
			t.Fatal("concurrent instance HTTP failure")
		}
		instanceCodes[result.code]++
	}
	if instanceCodes[common.CodeSuccess] != 1 || instanceCodes[common.CodeConflict] != 1 {
		t.Fatal("concurrent instance creation was not serialized")
	}
	if err := read.QueryRow("SELECT count(*) FROM tenant_model_instance WHERE provider_id=$1 AND instance_name=$2", providerID, duplicateInstance).Scan(&stored); err != nil || stored != 1 {
		t.Fatal("duplicate instance SQL rows")
	}
	command, err := cli.NewParser("ADD MODEL 'cli-zero' TO PROVIDER 'vllm' INSTANCE '" + instanceName + "' WITH TOKENS 0 CHAT VISION;").Parse(false)
	if err != nil {
		t.Fatal("CLI zero declaration parse failed")
	}
	response, err := consumer.ExecuteCommand(command)
	if err != nil || response == nil {
		t.Fatal("actual single-model CLI consumer failed")
	}
	if err := read.QueryRow("SELECT extra FROM tenant_model WHERE instance_id=$1 AND model_name='cli-zero'", instanceID).Scan(&raw); err != nil {
		t.Fatal("CLI model SQL readback failed")
	}
	if json.Unmarshal([]byte(raw), &extra) != nil || extra.MaxTokens != 0 || !reflect.DeepEqual(extra.ModelTypes, []string{"chat", "image2text"}) {
		t.Fatal("CLI zero or capability payload changed")
	}
	if _, err := consumer.ExecuteCommand(command); err == nil {
		t.Fatal("CLI ignored duplicate business error")
	}
	var afterExtra string
	if err := read.QueryRow("SELECT extra FROM tenant_model_instance WHERE id=$1", instanceID).Scan(&afterExtra); err != nil || afterExtra != instanceExtra {
		t.Fatal("model declarations changed instance URL or region")
	}
	journal.Event("model-contract-readback", map[string]any{"provider_id": providerID, "instance_id": instanceID, "multi_types": []string{"chat", "image2text", "embedding"}, "max_tokens": 0, "concurrent_success": counts[common.CodeSuccess], "concurrent_conflict": counts[common.CodeConflict], "cli_single_request": true, "retired_routes_absent": true})
}
