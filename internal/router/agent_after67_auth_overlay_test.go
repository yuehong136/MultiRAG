package router

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"mime/multipart"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v4"
	"github.com/google/uuid"
	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/storage"
	"multirag/internal/testsupport"
	"multirag/internal/utility"
)

type routerAgentCaseStore struct {
	storage.Storage
	strict interface {
		ObjectExistsContext(context.Context, string, string) (bool, error)
	}
	journal      *testsupport.CaseJournal
	bucket       string
	mu           sync.Mutex
	keys         map[string]bool
	puts, failAt int
	calls        int
}

func (s *routerAgentCaseStore) callCount() int { s.mu.Lock(); defer s.mu.Unlock(); return s.calls }
func (s *routerAgentCaseStore) called()        { s.mu.Lock(); s.calls++; s.mu.Unlock() }
func (s *routerAgentCaseStore) Get(bucket, key string, tenantID ...string) ([]byte, error) {
	s.called()
	return s.Storage.Get(bucket, key, tenantID...)
}
func (s *routerAgentCaseStore) Remove(bucket, key string, tenantID ...string) error {
	s.called()
	return s.Storage.Remove(bucket, key, tenantID...)
}
func (s *routerAgentCaseStore) RemoveContext(ctx context.Context, bucket, key string, tenantID ...string) error {
	s.called()
	return s.Storage.(interface {
		RemoveContext(context.Context, string, string, ...string) error
	}).RemoveContext(ctx, bucket, key, tenantID...)
}
func (s *routerAgentCaseStore) reserve(bucket, key string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	identity := bucket + "/" + key
	if !s.keys[identity] {
		s.journal.Event("reserve-object", map[string]any{"physical_bucket": s.bucket, "physical_key": identity, "logical_bucket": bucket, "logical_key": key})
		s.keys[identity] = true
	}
}
func (s *routerAgentCaseStore) ObjectExistsContext(ctx context.Context, bucket, key string) (bool, error) {
	s.called()
	s.reserve(bucket, key)
	return s.strict.ObjectExistsContext(ctx, bucket, key)
}
func (s *routerAgentCaseStore) Put(bucket, key string, data []byte, tenantID ...string) error {
	s.called()
	s.reserve(bucket, key)
	err := s.Storage.Put(bucket, key, data, tenantID...)
	s.mu.Lock()
	s.puts++
	fail := s.failAt > 0 && s.puts == s.failAt
	s.mu.Unlock()
	if err == nil && fail {
		return errors.New("injected persisted write failure")
	}
	return err
}

// This final-SHA overlay drives real Router.Setup and JWT/API authentication.
// A transparent MinIO wrapper registers keys and one persisted-write failure boundary.
func TestAgentAfter67RouterJWTAPI(t *testing.T) {
	path := os.Getenv("MULTIRAG_AFTER67_A_ROUTER_CONFIG")
	if path == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("private Agent scratch config required")
		}
		t.Skip("requires Agent scratch PostgreSQL/MinIO")
	}
	journal := testsupport.NewCase(t)
	t.Setenv("MAX_FILE_NUM_PER_USER", "0")
	if len(os.Getenv("MULTIRAG_AFTER67_GO_SHA")) != 40 {
		t.Fatal("explicit final Go SHA required")
	}
	keyPath := filepath.Join(os.Getenv("TMPDIR"), "after67-a-router-key-"+uuid.NewString()+".yaml")
	journal.Event("reserve-secret-config", map[string]any{"path": keyPath})
	if err := os.WriteFile(keyPath, []byte("multirag:\n  secret_key: "+uuid.NewString()+uuid.NewString()+"\n"), 0600); err != nil {
		t.Fatal("write owned signing config")
	}
	t.Cleanup(func() {
		if err := os.Remove(keyPath); err != nil {
			t.Error(err)
		}
		_, err := os.Stat(keyPath)
		journal.Event("cleanup-secret-config", map[string]any{"path": keyPath, "absent": os.IsNotExist(err)})
		if !os.IsNotExist(err) {
			t.Error("signing config survived")
		}
	})
	previousKey := server.GetSecretKey()
	if err := server.FromConfigFile(keyPath); err != nil {
		t.Fatal("read owned signing config")
	}
	if err := server.InitVariables(nil); err != nil {
		t.Fatal("initialize signing configuration")
	}
	t.Cleanup(func() { server.SetSecretKey(previousKey) })
	previousAdmin := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned after67 agent router acceptance")
	t.Cleanup(func() { local.SetAdminStatus(previousAdmin.Status, previousAdmin.Reason) })
	journal.Event("frozen-go-source", map[string]any{"sha": os.Getenv("MULTIRAG_AFTER67_GO_SHA"), "router": "Router.Setup", "auth": "actual signed JWT and SQL API-token middleware"})
	var cfg struct {
		AdminDSN   string             `json:"admin_dsn"`
		PGEndpoint string             `json:"pg_endpoint"`
		Minio      server.MinioConfig `json:"minio"`
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err = json.Unmarshal(raw, &cfg); err != nil {
		t.Fatal(err)
	}
	parsed, err := url.Parse(cfg.AdminDSN)
	if err != nil || parsed.Host != "127.0.0.1:5432" || cfg.PGEndpoint != parsed.Host {
		t.Fatal("explicit TCP endpoint required")
	}
	admin, err := sql.Open("pgx", cfg.AdminDSN)
	if err != nil {
		t.Fatal("open admin")
	}
	t.Cleanup(func() { admin.Close() })
	suffix := strings.ReplaceAll(uuid.NewString(), "-", "")[:16]
	name := "multirag_after67_a_router_" + suffix
	journal.Event("reserve-database", map[string]any{"endpoint": cfg.PGEndpoint, "database": name, "config_file": path})
	var exists bool
	if err = admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&exists); err != nil || exists {
		t.Fatal("scratch database precondition", err)
	}
	if _, err = admin.Exec(`CREATE DATABASE "` + name + `"`); err != nil {
		t.Fatal("create scratch database", err)
	}
	t.Cleanup(func() {
		if _, err := admin.Exec(`DROP DATABASE "` + name + `" WITH (FORCE)`); err != nil {
			t.Error("drop scratch", err)
		}
		var present bool
		readErr := admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&present)
		if readErr != nil || present {
			t.Error("scratch database cleanup readback", readErr)
		}
		journal.Event("cleanup-database", map[string]any{"endpoint": cfg.PGEndpoint, "database": name, "absent": readErr == nil && !present})
	})
	parsed.Path = "/" + name
	query := parsed.Query()
	query.Set("search_path", "usr_ai,public")
	parsed.RawQuery = query.Encode()
	db, err := gorm.Open(postgres.Open(parsed.String()), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("connect scratch", err)
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	read, err := sql.Open("pgx", parsed.String())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { read.Close() })
	previous := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = previous })
	journal.Event("reserve-schema", map[string]any{"database": name, "schema": "usr_ai", "tables": []string{"t_ai_users", "t_ai_tenants", "t_ai_user_tenants", "t_ai_api_tokens", "t_ai_user_canvases", "t_ai_user_canvas_version", "t_ai_files", "t_ai_knowledgebases", "t_ai_documents"}})
	if err = db.Exec("CREATE SCHEMA usr_ai").Error; err != nil {
		t.Fatal("create owned usr_ai schema", err)
	}
	if err = db.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.APIToken{}, &entity.UserCanvas{}, &entity.UserCanvasVersion{}, &entity.File{}, &entity.Knowledgebase{}, &entity.Document{}); err != nil {
		t.Fatal(err)
	}
	active, inactive := "1", "0"
	credentialsByAlias := map[string]string{}
	for _, id := range []string{"owner", "normal", "admin", "invite", "revoked", "foreign", "disabled", "super", "session-revoked", "tenant-off"} {
		session := "session-" + uuid.NewString()
		if id == "session-revoked" {
			session = "INVALID_" + uuid.NewString()
		}
		user := entity.User{ID: id, Email: id + "@agent.test", Nickname: id, Status: &active, AccessToken: &session}
		if id == "super" {
			yes := true
			user.IsSuperuser = &yes
		}
		journal.Event("reserve-user", map[string]any{"user_id": id})
		if err = db.Create(&user).Error; err != nil {
			t.Fatal("seed authenticated user", err)
		}
		credential, err := utility.SignUserJWT(user.Email, server.GetSecretKey())
		if err != nil {
			t.Fatal("sign owned JWT")
		}
		credentialsByAlias[id] = credential
		token := "multirag-" + strings.ReplaceAll(uuid.NewString(), "-", "")
		beta := "beta-" + strings.ReplaceAll(uuid.NewString(), "-", "")
		tokenHash, betaHash := sha256.Sum256([]byte(token)), sha256.Sum256([]byte(beta))
		journal.Event("reserve-auth-identities", map[string]any{"user_id": id, "api_token_sha256": hex.EncodeToString(tokenHash[:]), "beta_sha256": hex.EncodeToString(betaHash[:])})
		if err = db.Create(&entity.APIToken{TenantID: id, Token: token, Beta: &beta}).Error; err != nil {
			t.Fatal("seed API token")
		}
		credentialsByAlias["api-"+id] = token
		if id == "owner" {
			credentialsByAlias["beta"] = beta
		}
		status := &active
		if id == "tenant-off" {
			status = &inactive
		}
		if err = db.Create(&entity.Tenant{ID: id, Status: status}).Error; err != nil {
			t.Fatal("seed tenant", err)
		}
	}
	if err = db.Model(&entity.User{}).Where("id = ?", "disabled").Update("is_active", false).Error; err != nil {
		t.Fatal(err)
	}
	credentialsByAlias["invalid"] = "invalid"
	credentialsByAlias["wrong-signature"], err = utility.SignUserJWT("owner@agent.test", "wrong-owned-signing-key")
	if err != nil {
		t.Fatal(err)
	}
	credentialsByAlias["expired"], err = jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.RegisteredClaims{Subject: "owner@agent.test", ExpiresAt: jwt.NewNumericDate(time.Now().Add(-time.Hour))}).SignedString([]byte(server.GetSecretKey()))
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range []struct {
		id, user, tenant, role string
		status                 *string
	}{
		{"normal", "normal", "owner", "normal", &active}, {"admin", "admin", "owner", "admin", &active}, {"invite", "invite", "owner", "invite", &active}, {"revoked", "revoked", "owner", "normal", &inactive},
		{"normal-disabled", "normal", "disabled", "normal", &active}, {"normal-tenant-off", "normal", "tenant-off", "normal", &active},
	} {
		if err = db.Create(&entity.UserTenant{ID: entry.id, UserID: entry.user, TenantID: entry.tenant, Role: entry.role, Status: entry.status}).Error; err != nil {
			t.Fatal("seed access membership", err)
		}
	}
	for _, canvas := range []entity.UserCanvas{{ID: "shared", UserID: "owner", Permission: "team"}, {ID: "private", UserID: "owner", Permission: "me"}, {ID: "empty", UserID: "owner"}, {ID: "disabled-canvas", UserID: "disabled", Permission: "team"}, {ID: "inactive-tenant-canvas", UserID: "tenant-off", Permission: "team"}} {
		if err = db.Create(&canvas).Error; err != nil {
			t.Fatal("seed canvas", err)
		}
	}
	stamp := int64(1777777777000)
	for _, v := range []entity.UserCanvasVersion{{ID: "v1", UserCanvasID: "shared", Release: false, DSL: entity.JSONMap{"secret": "draft"}, BaseModel: entity.BaseModel{UpdateTime: &stamp}}, {ID: "v2", UserCanvasID: "shared", Release: true, DSL: entity.JSONMap{"value": "published"}, BaseModel: entity.BaseModel{UpdateTime: &stamp}}, {ID: "foreign-version", UserCanvasID: "private", Release: true, DSL: entity.JSONMap{"secret": "private"}}} {
		if err = db.Create(&v).Error; err != nil {
			t.Fatal(err)
		}
	}
	bucket := "multirag-after67-a-router-" + suffix
	journal.Event("reserve-bucket", map[string]any{"endpoint": cfg.Minio.Host, "bucket": bucket, "prefix": "", "secure": cfg.Minio.Secure})
	client, err := minio.New(cfg.Minio.Host, &minio.Options{Creds: credentials.NewStaticV4(cfg.Minio.User, cfg.Minio.Password, ""), Secure: cfg.Minio.Secure, Region: cfg.Minio.Region})
	if err != nil {
		t.Fatal("create isolated client", err)
	}
	ctx := context.Background()
	if exists, err := client.BucketExists(ctx, bucket); err != nil || exists {
		t.Fatal("bucket precondition", err)
	}
	if err = client.MakeBucket(ctx, bucket, minio.MakeBucketOptions{Region: cfg.Minio.Region}); err != nil {
		t.Fatal("create owned bucket", err)
	}
	t.Cleanup(func() {
		for object := range client.ListObjects(ctx, bucket, minio.ListObjectsOptions{Recursive: true}) {
			if object.Err != nil {
				t.Error(object.Err)
				continue
			}
			if err := client.RemoveObject(ctx, bucket, object.Key, minio.RemoveObjectOptions{}); err != nil {
				t.Error(err)
			}
		}
		if err := client.RemoveBucket(ctx, bucket); err != nil {
			t.Error(err)
		}
		present, err := client.BucketExists(ctx, bucket)
		if err != nil || present {
			t.Error("owned bucket survived cleanup", err)
		}
		journal.Event("cleanup-bucket", map[string]any{"endpoint": cfg.Minio.Host, "bucket": bucket, "absent": err == nil && !present})
	})
	cfg.Minio.Bucket = bucket
	cfg.Minio.PrefixPath = ""
	concrete, err := storage.NewMinioStorage(&cfg.Minio)
	if err != nil {
		t.Fatal(err)
	}
	store := &routerAgentCaseStore{Storage: concrete, strict: concrete, journal: journal, bucket: bucket, keys: map[string]bool{}}
	previousStore := storage.GetStorageFactory().GetStorage()
	storage.GetStorageFactory().SetStorage(store)
	t.Cleanup(func() { storage.GetStorageFactory().SetStorage(previousStore) })
	gin.SetMode(gin.TestMode)
	engine := gin.New()
	// Use the real public constructor and the actual Setup fallback AgentHandler.
	r := NewRouter(handler.NewAuthHandler(), nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil)
	r.Setup(engine)
	for _, expected := range []string{"GET /api/v1/agents/:agent_id/versions", "GET /api/v1/agents/:agent_id/versions/:version_id", "POST /api/v1/agents/:agent_id/upload"} {
		registered := 0
		for _, route := range engine.Routes() {
			if route.Method+" "+route.Path == expected {
				registered++
			}
		}
		if registered != 1 {
			t.Fatalf("expected one current Router registration for %s, got %d", expected, registered)
		}
	}
	address := "127.0.0.1:18877"
	journal.Event("reserve-http-listener", map[string]any{"address": address})
	listener, err := net.Listen("tcp", address)
	if err != nil {
		t.Fatal("reserve exact listener", err)
	}
	httpServer := &httptest.Server{Listener: listener, Config: &http.Server{Handler: engine}}
	httpServer.Start()
	t.Cleanup(func() {
		httpServer.Close()
		conn, err := net.DialTimeout("tcp", address, 200*time.Millisecond)
		if err == nil {
			conn.Close()
			t.Error("HTTP listener survived cleanup")
		}
		journal.Event("cleanup-listener", map[string]any{"address": address, "absent": err != nil})
	})
	transport := http.DefaultTransport.(*http.Transport).Clone()
	t.Cleanup(transport.CloseIdleConnections)
	httpClient := &http.Client{Timeout: 10 * time.Second, Transport: transport}
	request := func(method, path, actor string, body io.Reader, contentType string) map[string]any {
		t.Helper()
		req, err := http.NewRequest(method, httpServer.URL+path, body)
		if err != nil {
			t.Fatal(err)
		}
		if actor != "" {
			token, ok := credentialsByAlias[actor]
			if !ok {
				t.Fatal("unknown credential alias")
			}
			req.Header.Set("Authorization", "Bearer "+token)
		}
		if contentType != "" {
			req.Header.Set("Content-Type", contentType)
		}
		res, err := httpClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		var value map[string]any
		if err = json.NewDecoder(res.Body).Decode(&value); err != nil {
			t.Fatal(err)
		}
		value["_http_status"] = res.StatusCode
		return value
	}
	checkHTTP := func(value map[string]any, status int, code common.ErrorCode) {
		t.Helper()
		if value["_http_status"] != status || value["code"] != float64(code) {
			t.Fatalf("HTTP/business code want %d/%d got %v/%v", status, code, value["_http_status"], value["code"])
		}
	}
	check := func(value map[string]any, code common.ErrorCode) { t.Helper(); checkHTTP(value, 200, code) }
	listPath := "/api/v1/agents/shared/versions"
	for _, actor := range []string{"owner", "normal", "admin", "api-owner", "api-normal", "api-admin"} {
		v := request("GET", listPath, actor, nil, "")
		check(v, common.CodeSuccess)
		rows := v["data"].([]any)
		if len(rows) != 2 || rows[0].(map[string]any)["id"] != "v2" || rows[0].(map[string]any)["release"] != true {
			t.Fatal("stable published summaries", v)
		}
		if _, ok := rows[0].(map[string]any)["dsl"]; ok {
			t.Fatal("summary exposed DSL")
		}
	}
	empty := request("GET", "/api/v1/agents/empty/versions", "owner", nil, "")
	check(empty, common.CodeSuccess)
	if len(empty["data"].([]any)) != 0 {
		t.Fatal("empty not []")
	}
	detail := request("GET", listPath+"/v2", "normal", nil, "")
	check(detail, common.CodeSuccess)
	if detail["data"].(map[string]any)["dsl"].(map[string]any)["value"] != "published" {
		t.Fatal("detail lost DSL")
	}
	for _, actor := range []string{"api-owner", "api-normal", "api-admin"} {
		v := request("GET", listPath+"/v2", actor, nil, "")
		check(v, common.CodeSuccess)
		if v["data"].(map[string]any)["dsl"].(map[string]any)["value"] != "published" {
			t.Fatal("API detail lost authorized DSL")
		}
	}
	for _, id := range []string{"foreign-version", "missing"} {
		check(request("GET", listPath+"/"+id, "normal", nil, ""), common.CodeNotFound)
	}
	countObjects := func() int {
		t.Helper()
		n := 0
		for o := range client.ListObjects(ctx, bucket, minio.ListObjectsOptions{Recursive: true}) {
			if o.Err != nil {
				t.Fatal(o.Err)
			}
			n++
		}
		return n
	}
	objectsDigest := func() string {
		t.Helper()
		hashes := map[string]string{}
		for object := range client.ListObjects(ctx, bucket, minio.ListObjectsOptions{Recursive: true}) {
			if object.Err != nil {
				t.Fatal("independent object snapshot unavailable", object.Err)
			}
			reader, err := client.GetObject(ctx, bucket, object.Key, minio.GetObjectOptions{})
			if err != nil {
				t.Fatal(err)
			}
			bytes, err := io.ReadAll(reader)
			closeErr := reader.Close()
			if err != nil || closeErr != nil {
				t.Fatal("independent object snapshot unreadable")
			}
			hash := sha256.Sum256(bytes)
			hashes[object.Key] = hex.EncodeToString(hash[:])
		}
		raw, err := json.Marshal(hashes)
		if err != nil {
			t.Fatal(err)
		}
		hash := sha256.Sum256(raw)
		return hex.EncodeToString(hash[:])
	}
	snapshot := func() string {
		t.Helper()
		var value string
		if err := read.QueryRow(`SELECT json_build_object('versions',(SELECT json_agg(to_jsonb(v) ORDER BY id) FROM t_ai_user_canvas_version v),'canvases',(SELECT json_agg(to_jsonb(c) ORDER BY id) FROM t_ai_user_canvases c),'files',(SELECT json_agg(to_jsonb(f) ORDER BY id) FROM t_ai_files f),'datasets',(SELECT json_agg(to_jsonb(k) ORDER BY id) FROM t_ai_knowledgebases k),'documents',(SELECT json_agg(to_jsonb(d) ORDER BY id) FROM t_ai_documents d),'users',(SELECT json_agg(to_jsonb(u) ORDER BY id) FROM t_ai_users u),'tenants',(SELECT json_agg(to_jsonb(t) ORDER BY id) FROM t_ai_tenants t),'memberships',(SELECT json_agg(to_jsonb(m) ORDER BY id) FROM t_ai_user_tenants m),'tokens',(SELECT json_agg(to_jsonb(a) ORDER BY tenant_id,token) FROM t_ai_api_tokens a))::text`).Scan(&value); err != nil {
			t.Fatal(err)
		}
		hash := sha256.Sum256([]byte(value))
		return hex.EncodeToString(hash[:])
	}
	beforeSQL := snapshot()
	for _, entry := range []struct{ actor, canvas string }{{"foreign", "shared"}, {"invite", "shared"}, {"revoked", "shared"}, {"normal", "private"}, {"normal", "disabled-canvas"}, {"normal", "inactive-tenant-canvas"}, {"api-foreign", "shared"}, {"api-invite", "shared"}, {"api-revoked", "shared"}, {"api-normal", "private"}, {"owner", "missing"}} {
		beforeCalls := store.callCount()
		check(request("GET", "/api/v1/agents/"+entry.canvas+"/versions", entry.actor, nil, ""), common.CodeOperatingError)
		check(request("GET", "/api/v1/agents/"+entry.canvas+"/versions/v2", entry.actor, nil, ""), common.CodeOperatingError)
		check(request("POST", "/api/v1/agents/"+entry.canvas+"/upload", entry.actor, strings.NewReader("malformed"), "multipart/form-data; boundary=x"), common.CodeOperatingError)
		if store.callCount() != beforeCalls || countObjects() != 0 || snapshot() != beforeSQL {
			t.Fatal("resource denial changed SQL/storage or reached storage")
		}
	}
	for _, entry := range []struct {
		alias  string
		status int
		code   common.ErrorCode
	}{
		{"", 401, common.CodeUnauthorized}, {"invalid", 401, common.CodeUnauthorized}, {"wrong-signature", 401, common.CodeUnauthorized}, {"expired", 401, common.CodeUnauthorized}, {"disabled", 401, common.CodeUnauthorized}, {"api-disabled", 401, common.CodeUnauthorized}, {"session-revoked", 401, common.CodeUnauthorized}, {"beta", 401, common.CodeUnauthorized}, {"super", 403, common.CodeForbidden}, {"api-super", 403, common.CodeForbidden},
	} {
		t.Run("auth-"+entry.alias, func(t *testing.T) {
			beforeCalls := store.callCount()
			for _, path := range []string{listPath, listPath + "/v2"} {
				checkHTTP(request("GET", path, entry.alias, nil, ""), entry.status, entry.code)
			}
			checkHTTP(request("POST", "/api/v1/agents/shared/upload", entry.alias, strings.NewReader("not parsed"), "multipart/form-data; boundary=x"), entry.status, entry.code)
			if countObjects() != 0 || snapshot() != beforeSQL || store.callCount() != beforeCalls {
				t.Fatal("authentication denial changed SQL/storage or reached storage")
			}
		})
	}

	if countObjects() != 0 || snapshot() != beforeSQL || store.callCount() != 0 {
		t.Fatal("denial had side effects or reached storage")
	}
	multipartBody := func(files []string) (*bytes.Buffer, string) {
		t.Helper()
		body := new(bytes.Buffer)
		w := multipart.NewWriter(body)
		for _, data := range files {
			part, err := w.CreateFormFile("file", "same.txt")
			if err != nil {
				t.Fatal(err)
			}
			if _, err = io.WriteString(part, data); err != nil {
				t.Fatal(err)
			}
		}
		if err = w.Close(); err != nil {
			t.Fatal(err)
		}
		return body, w.FormDataContentType()
	}
	for _, actor := range []string{"owner", "normal", "admin", "api-owner", "api-normal", "api-admin"} {
		actorID := strings.TrimPrefix(actor, "api-")
		body, ct := multipartBody([]string{"one", "two"})
		v := request("POST", "/api/v1/agents/shared/upload", actor, body, ct)
		check(v, common.CodeSuccess)
		rows := v["data"].([]any)
		if len(rows) != 2 {
			t.Fatal("repeated file loss")
		}
		for i, item := range rows {
			d := item.(map[string]any)
			id := d["id"].(string)
			key := actorID + "-downloads/" + id
			if d["created_by"] != actorID || d["preview_url"] != nil {
				t.Fatal("wrong descriptor owner", d)
			}
			object, err := client.GetObject(ctx, bucket, key, minio.GetObjectOptions{})
			if err != nil {
				t.Fatal(err)
			}
			data, err := io.ReadAll(object)
			object.Close()
			if err != nil || string(data) != []string{"one", "two"}[i] {
				t.Fatal("independent object byte readback", err)
			}
			meta, err := client.GetObject(ctx, bucket, key+".upload.json", minio.GetObjectOptions{})
			if err != nil {
				t.Fatal(err)
			}
			var persisted map[string]any
			err = json.NewDecoder(meta).Decode(&persisted)
			meta.Close()
			if err != nil || persisted["created_by"] != actorID || persisted["id"] != id {
				t.Fatal("independent descriptor readback", err)
			}
		}
	}

	// Quota follows the tenant's KB, including a document created by another actor.
	if err = db.Create(&entity.Knowledgebase{ID: "quota-kb", TenantID: "owner", CreatedBy: "owner", ParserConfig: entity.JSONMap{}}).Error; err != nil {
		t.Fatal(err)
	}
	if err = db.Create(&entity.Document{ID: "quota-doc", KbID: "quota-kb", CreatedBy: "foreign", ParserConfig: entity.JSONMap{}}).Error; err != nil {
		t.Fatal(err)
	}
	beforeSQL = snapshot()
	t.Setenv("MAX_FILE_NUM_PER_USER", "1")
	quotaBefore := countObjects()
	quotaBeforeCalls := store.callCount()
	quotaBody, quotaCT := multipartBody([]string{"over quota"})
	check(request("POST", "/api/v1/agents/shared/upload", "owner", quotaBody, quotaCT), common.CodeOperatingError)
	if countObjects() != quotaBefore || snapshot() != beforeSQL || store.callCount() != quotaBeforeCalls {
		t.Fatal("quota rejection wrote SQL/objects or reached storage")
	}
	t.Setenv("MAX_FILE_NUM_PER_USER", "0")
	baseline := countObjects()
	body, ct := multipartBody([]string{"single"})
	single := request("POST", "/api/v1/agents/shared/upload", "normal", body, ct)
	check(single, common.CodeSuccess)
	if _, ok := single["data"].(map[string]any); !ok {
		t.Fatal("single descriptor shape")
	}
	if countObjects() != baseline+2 {
		t.Fatal("single key pair")
	}
	baseline = countObjects()
	beforeFailureObjects := objectsDigest()
	store.mu.Lock()
	store.failAt = store.puts + 4
	store.mu.Unlock()
	body, ct = multipartBody([]string{"first", "second"})
	check(request("POST", "/api/v1/agents/shared/upload", "normal", body, ct), common.CodeServerError)
	if countObjects() != baseline || objectsDigest() != beforeFailureObjects {
		t.Fatal("failed batch left keys or changed prior object bytes/descriptors")
	}
	store.mu.Lock()
	store.failAt = 0
	store.mu.Unlock()
	body, ct = multipartBody(nil)
	check(request("POST", "/api/v1/agents/shared/upload", "owner", body, ct), common.CodeArgumentError)
	body, ct = multipartBody([]string{"blocked"})
	check(request("POST", "/api/v1/agents/shared/upload?url=https://example.invalid", "owner", body, ct), common.CodeArgumentError)
	if countObjects() != baseline || snapshot() != beforeSQL {
		t.Fatal("invalid upload mutated SQL/storage")
	}
	// Query parsing may discard malformed escapes; raw query presence must reject
	// before multipart parsing or writes, including the bare query marker.
	for _, query := range []string{"?url=%ZZ", "?url=%", "?"} {
		callsBeforeQuery := store.callCount()
		body, ct = multipartBody([]string{"must not persist"})
		check(request("POST", "/api/v1/agents/shared/upload"+query, "owner", body, ct), common.CodeArgumentError)
		if countObjects() != baseline || store.callCount() != callsBeforeQuery || snapshot() != beforeSQL {
			t.Fatal("raw query rejection changed SQL/storage or reached storage", query)
		}
	}
	var fileCount int
	if err = read.QueryRow("SELECT count(*) FROM t_ai_files").Scan(&fileCount); err != nil || fileCount != 0 {
		t.Fatal("runtime upload entered File tree", err)
	}
	journal.Event("acceptance-readback", map[string]any{"database": name, "version_count": 3, "router_registered_routes": 3, "JWT_API_success_principals": 6, "public_beta_granted_principal": false, "ordinary_file_rows": fileCount, "object_count": baseline, "failed_batch_residue": 0, "denial_side_effects": 0, "denial_storage_calls": 0, "http_principal": "actual Router.Setup/AuthMiddleware JWT and API token", "store": "real MinIO"})
	t.Log("final Router.Setup/JWT/API real HTTP+PostgreSQL+MinIO: owner/normal/admin, auth+business denials, beta rejected, stable published summaries, version binding, actor-owned repeated/single uploads, exact persisted-write compensation and independent SQL/blob/descriptor readback")
}
