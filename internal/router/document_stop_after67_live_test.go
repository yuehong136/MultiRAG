package router

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/testsupport"
	"multirag/internal/utility"
)

// TestDocumentStopAfter67Live exercises the current registered Router and real
// auth middleware over TCP HTTP. No principal or storage boundary is replaced.
func TestDocumentStopAfter67Live(t *testing.T) {
	path := os.Getenv("MULTIRAG_AFTER67_G_CONFIG")
	if path == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned config required")
		}
		t.Skip("owned config required")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("private config unavailable")
	}
	var cfg struct {
		Database server.DatabaseConfig
		Redis    server.RedisConfig
	}
	if json.Unmarshal(raw, &cfg) != nil {
		t.Fatal("invalid private config")
	}
	redisHost := cfg.Redis.Host
	if host, port, err := net.SplitHostPort(redisHost); err == nil {
		if port != "6379" {
			t.Fatal("unowned Redis endpoint")
		}
		redisHost = host
	}
	if cfg.Database.Host != "127.0.0.1" || cfg.Database.Port != 5432 || cfg.Database.Database != "multirag_batch_g_after67_20261029" || cfg.Database.Schema != "usr_ai" || redisHost != "127.0.0.1" || cfg.Redis.Port != 6379 || cfg.Redis.DB != 1 {
		t.Fatal("unowned service endpoint")
	}
	journal := testsupport.NewCase(t)
	journal.Event("borrow-services", map[string]any{"database": cfg.Database.Database, "sql_host": cfg.Database.Host, "sql_port": cfg.Database.Port, "schema": cfg.Database.Schema, "redis_host": redisHost, "redis_port": cfg.Redis.Port, "redis_db": cfg.Redis.DB})
	configPath := filepath.Join(os.Getenv("TMPDIR"), "after67-g-router-"+uuid.NewString()+".yaml")
	journal.Event("reserve-secret-config", map[string]any{"path": configPath})
	if err := os.WriteFile(configPath, []byte("multirag:\n  secret_key: "+uuid.NewString()+uuid.NewString()+"\n"), 0600); err != nil {
		t.Fatal("secret config write")
	}
	t.Cleanup(func() {
		if err := os.Remove(configPath); err != nil {
			t.Error("secret config cleanup")
		}
		_, err := os.Stat(configPath)
		journal.Event("secret-config-cleanup-readback", map[string]any{"path": configPath, "absent": os.IsNotExist(err)})
		if !os.IsNotExist(err) {
			t.Error("secret config survived")
		}
	})
	previousKey := server.GetSecretKey()
	if err := server.FromConfigFile(configPath); err != nil {
		t.Fatal("config load")
	}
	if err := server.InitVariables(nil); err != nil {
		t.Fatal("variable init")
	}
	t.Cleanup(func() { server.SetSecretKey(previousKey) })
	dsn, err := dao.PostgresDSN(cfg.Database)
	if err != nil {
		t.Fatal("owned SQL config")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("owned SQL unavailable")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	reader, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("independent SQL unavailable")
	}
	t.Cleanup(func() { reader.Close() })
	client := redis.NewClient(&redis.Options{Addr: "127.0.0.1:6379", DB: 1, Password: cfg.Redis.Password})
	t.Cleanup(func() { client.Close() })
	if client.Ping(context.Background()).Err() != nil {
		t.Fatal("owned Redis unavailable")
	}
	oldDB := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = oldDB })
	tables := []string{"t_ai_users", "t_ai_tenants", "t_ai_user_tenants", "t_ai_api_tokens", "t_ai_knowledgebases", "t_ai_documents", "t_ai_tasks"}
	journal.Event("reserve-schema", map[string]any{"database": cfg.Database.Database, "tables": tables})
	if db.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.APIToken{}, &entity.Knowledgebase{}, &entity.Document{}, &entity.Task{}) != nil {
		t.Fatal("owned schema unavailable")
	}
	prefix := "g67" + strings.ReplaceAll(uuid.NewString(), "-", "")[:15]
	owner, stranger, inactive, revoked, super := prefix+"owner", prefix+"other", prefix+"off", prefix+"revoked", prefix+"super"
	kb, otherKB, a, b, foreign, taskA, taskB := prefix+"kb", prefix+"otherkb", prefix+"a", prefix+"b", prefix+"foreign", prefix+"ta", prefix+"tb"
	users := []string{owner, stranger, inactive, revoked, super}
	kbs := []string{kb, otherKB}
	docs := []string{a, b, foreign}
	tasks := []string{taskA, taskB}
	members := []string{prefix + "mo", prefix + "ms"}
	keys := []string{taskA + "-cancel", taskB + "-cancel", "task-runtime:v1:" + taskA, "task-runtime:v1:" + taskB}
	apiToken := "multirag-" + strings.ReplaceAll(uuid.NewString(), "-", "")
	tokenHash := sha256.Sum256([]byte(apiToken))
	journal.Event("reserve-identities", map[string]any{"users": users, "tenants": []string{owner, stranger}, "memberships": members, "datasets": kbs, "documents": docs, "tasks": tasks, "redis_keys": keys, "api_token_tenant": owner, "api_token_sha256": hex.EncodeToString(tokenHash[:])})
	t.Cleanup(func() {
		err := db.Transaction(func(tx *gorm.DB) error {
			for _, row := range []struct {
				model any
				where string
				ids   []string
			}{{&entity.Task{}, "id IN ?", tasks}, {&entity.Document{}, "id IN ?", docs}, {&entity.Knowledgebase{}, "id IN ?", kbs}, {&entity.APIToken{}, "tenant_id IN ?", []string{owner}}, {&entity.UserTenant{}, "id IN ?", members}, {&entity.Tenant{}, "id IN ?", []string{owner, stranger}}, {&entity.User{}, "id IN ?", users}} {
				if err := tx.Where(row.where, row.ids).Delete(row.model).Error; err != nil {
					return err
				}
			}
			return nil
		})
		if err != nil {
			t.Error("owned SQL cleanup failed")
		}
		if client.Del(context.Background(), keys...).Err() != nil {
			t.Error("owned Redis cleanup failed")
		}
		remaining := 0
		for _, table := range tables {
			var count int
			column := "id"
			ids := []string{a, b, foreign, taskA, taskB, kb, otherKB, owner, stranger, inactive, revoked, super, members[0], members[1]}
			if table == "t_ai_api_tokens" {
				column = "tenant_id"
			}
			holders := make([]string, len(ids))
			args := make([]any, len(ids))
			for i, id := range ids {
				holders[i] = fmt.Sprintf("$%d", i+1)
				args[i] = id
			}
			if err := reader.QueryRow("SELECT count(*) FROM "+table+" WHERE "+column+" IN ("+strings.Join(holders, ",")+")", args...).Scan(&count); err != nil {
				t.Error("SQL cleanup readback unavailable")
			} else {
				remaining += count
			}
		}
		redisRemaining, err := client.Exists(context.Background(), keys...).Result()
		if err != nil || redisRemaining != 0 || remaining != 0 {
			t.Error("owned cleanup readback nonzero")
		}
		journal.Event("cleanup-readback", map[string]any{"sql_remaining": remaining, "redis_remaining": redisRemaining, "borrowed_services_preserved": true})
	})
	active, disabled, session, invalidSession := "1", "0", "session-"+uuid.NewString(), "INVALID_"+uuid.NewString()
	yes := true
	jwt := map[string]string{}
	for _, id := range users {
		status, access := &active, &session
		if id == inactive {
			status = &disabled
		}
		if id == revoked {
			access = &invalidSession
		}
		user := entity.User{ID: id, Email: id + "@after67.invalid", Nickname: id, Status: status, AccessToken: access}
		if id == super {
			user.IsSuperuser = &yes
		}
		if db.Create(&user).Error != nil {
			t.Fatal("seed user")
		}
		jwt[id], err = utility.SignUserJWT(user.Email, server.GetSecretKey())
		if err != nil {
			t.Fatal("sign JWT")
		}
	}
	for _, id := range []string{owner, stranger} {
		if db.Create(&entity.Tenant{ID: id, Status: &active}).Error != nil {
			t.Fatal("seed tenant")
		}
	}
	for i, id := range []string{owner, stranger} {
		if db.Create(&entity.UserTenant{ID: members[i], UserID: id, TenantID: id, Role: "owner", Status: &active}).Error != nil {
			t.Fatal("seed membership")
		}
	}
	if db.Create(&entity.APIToken{TenantID: owner, Token: apiToken}).Error != nil {
		t.Fatal("seed API token")
	}
	for _, id := range kbs {
		tenant := owner
		if id == otherKB {
			tenant = stranger
		}
		if db.Create(&entity.Knowledgebase{ID: id, TenantID: tenant, CreatedBy: tenant, Name: id, Permission: "team", Status: &active, EmbdID: "", ParserConfig: entity.JSONMap{}}).Error != nil {
			t.Fatal("seed dataset")
		}
	}
	for _, id := range docs {
		dataset, creator := kb, owner
		if id == foreign {
			dataset, creator = otherKB, stranger
		}
		running := "1"
		if db.Create(&entity.Document{ID: id, KbID: dataset, CreatedBy: creator, Name: &id, ParserID: "naive", ParserConfig: entity.JSONMap{}, Status: &active, Run: &running}).Error != nil {
			t.Fatal("seed document")
		}
	}
	for i, id := range tasks {
		if db.Create(&entity.Task{ID: id, DocID: docs[i], Progress: 0.25}).Error != nil {
			t.Fatal("seed task")
		}
	}
	oldAdmin := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned after67 test")
	t.Cleanup(func() { local.SetAdminStatus(oldAdmin.Status, oldAdmin.Reason) })
	documents := &service.DocumentService{}
	documents.SetDocumentStopService(service.NewDocumentStopService(db, client))
	app := gin.New()
	(&Router{authHandler: handler.NewAuthHandler(), documentHandler: handler.NewDocumentHandler(documents)}).Setup(app)
	addr := "127.0.0.1:19967"
	journal.Event("reserve-listener", map[string]any{"address": addr})
	listener, err := net.Listen("tcp", addr)
	if err != nil {
		t.Fatal("bind registered listener")
	}
	api := &httptest.Server{Listener: listener, Config: &http.Server{Handler: app}}
	api.Start()
	t.Cleanup(func() {
		api.Close()
		c, err := net.DialTimeout("tcp", addr, time.Second)
		if err == nil {
			c.Close()
			t.Error("owned listener survived")
		}
		journal.Event("listener-cleanup-readback", map[string]any{"address": addr, "absent": err != nil})
	})
	snapshot := func() string {
		t.Helper()
		var value string
		if reader.QueryRow("SELECT json_build_object('docs',(SELECT json_agg(to_jsonb(d) ORDER BY id) FROM t_ai_documents d WHERE id IN ($1,$2,$3)),'tasks',(SELECT json_agg(to_jsonb(t) ORDER BY id) FROM t_ai_tasks t WHERE id IN ($4,$5)))::text", a, b, foreign, taskA, taskB).Scan(&value) != nil {
			t.Fatal("independent SQL snapshot")
		}
		return value
	}
	type response struct {
		Code int `json:"code"`
		Data struct {
			SuccessCount int             `json:"success_count"`
			Errors       json.RawMessage `json:"errors"`
		} `json:"data"`
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	t.Cleanup(transport.CloseIdleConnections)
	httpClient := &http.Client{Timeout: 5 * time.Second, Transport: transport}
	request := func(t *testing.T, auth, body string) (int, response) {
		t.Helper()
		req, err := http.NewRequest("POST", api.URL+"/api/v1/datasets/"+kb+"/documents/stop", strings.NewReader(body))
		if err != nil {
			t.Fatal("request build")
		}
		req.Header.Set("Content-Type", "application/json")
		if auth != "" {
			req.Header.Set("Authorization", "Bearer "+auth)
		}
		res, err := httpClient.Do(req)
		if err != nil {
			t.Fatal("HTTP request failed")
		}
		defer res.Body.Close()
		var value response
		if json.NewDecoder(res.Body).Decode(&value) != nil {
			t.Fatal("invalid HTTP envelope")
		}
		return res.StatusCode, value
	}
	body := fmt.Sprintf(`{"document_ids":[%q,%q,%q]}`, b, a, a)
	before := snapshot()
	for _, tc := range []struct {
		name, auth, body string
		status, code     int
	}{{"missing", "", body, 401, 401}, {"invalid", "invalid", body, 401, 401}, {"superuser", jwt[super], body, 403, 403}, {"inactive", jwt[inactive], body, 401, 401}, {"revoked", jwt[revoked], body, 401, 401}, {"stranger", jwt[stranger], body, 200, 403}, {"mixed_foreign", jwt[owner], fmt.Sprintf(`{"document_ids":[%q,%q]}`, a, foreign), 200, 403}, {"null", jwt[owner], `{"document_ids":null}`, 200, 400}, {"type", jwt[owner], `{"document_ids":[2]}`, 200, 400}, {"duplicate_key", jwt[owner], `{"document_ids":["a"],"document_ids":["b"]}`, 200, 400}} {
		t.Run(tc.name, func(t *testing.T) {
			status, result := request(t, tc.auth, tc.body)
			if status != tc.status || result.Code != tc.code || snapshot() != before {
				t.Fatalf("denial HTTP/code %d/%d; wanted %d/%d; SQL unchanged=%v", status, result.Code, tc.status, tc.code, snapshot() == before)
			}
			n, err := client.Exists(context.Background(), keys...).Result()
			if err != nil || n != 0 {
				t.Fatal("denied request wrote Redis state")
			}
		})
	}
	status, result := request(t, jwt[owner], body)
	if status != 200 || result.Code != 0 || result.Data.SuccessCount != 2 || len(result.Data.Errors) != 0 {
		t.Fatal("JWT cancellation acknowledgement incomplete")
	}
	var canceled int
	if reader.QueryRow("SELECT count(*) FROM t_ai_documents d JOIN t_ai_tasks t ON t.doc_id=d.id WHERE d.id IN ($1,$2) AND d.run='2' AND d.progress=0 AND t.progress=-1", a, b).Scan(&canceled) != nil || canceled != 2 {
		t.Fatal("SQL cancellation not committed")
	}
	nonces, err := client.MGet(context.Background(), taskA+"-cancel", taskB+"-cancel").Result()
	if err != nil || len(nonces) != 2 || nonces[0] == nil || nonces[1] == nil || nonces[0] == nonces[1] {
		t.Fatal("nonce readback invalid")
	}
	committed := snapshot()
	status, result = request(t, apiToken, body)
	after, err := client.MGet(context.Background(), taskA+"-cancel", taskB+"-cancel").Result()
	if err != nil || status != 200 || result.Code != 0 || result.Data.SuccessCount != 2 || snapshot() != committed || !reflect.DeepEqual(nonces, after) {
		t.Fatal("API-token cancellation retry changed committed state")
	}
	journal.Event("business-readback", map[string]any{"router": "Router.Setup", "auth": "real JWT and API token middleware", "http_listener": addr, "distinct_acknowledged": 2, "sql_canceled": canceled, "denials_zero_sql_redis_effects": true, "api_retry_preserved_nonces": true, "worker_termination_proven": false})
}
