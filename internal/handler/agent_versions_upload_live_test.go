package handler_test

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"mime/multipart"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
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
	"multirag/internal/service"
	"multirag/internal/storage"
	"multirag/internal/testsupport"
)

type agentCaseStore struct {
	storage.Storage
	strict interface {
		ObjectExistsContext(context.Context, string, string) (bool, error)
	}
	journal      *testsupport.CaseJournal
	bucket       string
	mu           sync.Mutex
	keys         map[string]bool
	puts, failAt int
}

func (s *agentCaseStore) reserve(bucket, key string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	identity := bucket + "/" + key
	if !s.keys[identity] {
		s.journal.Event("reserve-object", map[string]any{"physical_bucket": s.bucket, "physical_key": identity, "logical_bucket": bucket, "logical_key": key})
		s.keys[identity] = true
	}
}
func (s *agentCaseStore) ObjectExistsContext(ctx context.Context, bucket, key string) (bool, error) {
	s.reserve(bucket, key)
	return s.strict.ObjectExistsContext(ctx, bucket, key)
}
func (s *agentCaseStore) Put(bucket, key string, data []byte, tenantID ...string) error {
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

// This controlled HTTP test uses actual PostgreSQL and MinIO; principal middleware is a fixture.
// Router JWT/API authentication and Python public beta behavior are separate consumers.
func TestAgentVersionsUploadScratchPostgres(t *testing.T) {
	path := os.Getenv("MULTIRAG_AGENT_CASE_CONFIG")
	if path == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("private Agent scratch config required")
		}
		t.Skip("requires Agent scratch PostgreSQL/MinIO")
	}
	journal := testsupport.NewCase(t)
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
	name := "multirag_after67_a_" + suffix
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
		if err := admin.QueryRow("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", name).Scan(&present); err != nil || present {
			t.Error("scratch database cleanup readback", err)
		}
		journal.Event("cleanup-database", map[string]any{"endpoint": cfg.PGEndpoint, "database": name, "absent": !present})
	})
	parsed.Path = "/" + name
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
	if err = db.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.UserCanvas{}, &entity.UserCanvasVersion{}, &entity.File{}, &entity.Knowledgebase{}, &entity.Document{}); err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"owner", "normal", "admin", "invite", "revoked", "foreign", "disabled"} {
		if err = db.Create(&entity.User{ID: id, Email: id + "@agent.test", Nickname: id}).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err = db.Model(&entity.User{}).Where("id = ?", "disabled").Update("is_active", false).Error; err != nil {
		t.Fatal(err)
	}
	active := "1"
	inactive := "0"
	for _, id := range []string{"owner", "disabled"} {
		if err = db.Create(&entity.Tenant{ID: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, entry := range []struct {
		id, role string
		status   *string
	}{{"normal", "normal", &active}, {"admin", "admin", &active}, {"invite", "invite", &active}, {"revoked", "normal", &inactive}} {
		if err = db.Create(&entity.UserTenant{ID: entry.id, UserID: entry.id, TenantID: "owner", Role: entry.role, Status: entry.status}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, canvas := range []entity.UserCanvas{{ID: "shared", UserID: "owner", Permission: "team"}, {ID: "private", UserID: "owner", Permission: "me"}, {ID: "empty", UserID: "owner"}, {ID: "disabled-canvas", UserID: "disabled", Permission: "team"}} {
		if err = db.Create(&canvas).Error; err != nil {
			t.Fatal(err)
		}
	}
	stamp := int64(1777777777000)
	for _, v := range []entity.UserCanvasVersion{{ID: "v1", UserCanvasID: "shared", Release: false, DSL: entity.JSONMap{"secret": "draft"}, BaseModel: entity.BaseModel{UpdateTime: &stamp}}, {ID: "v2", UserCanvasID: "shared", Release: true, DSL: entity.JSONMap{"value": "published"}, BaseModel: entity.BaseModel{UpdateTime: &stamp}}, {ID: "foreign-version", UserCanvasID: "private", Release: true, DSL: entity.JSONMap{"secret": "private"}}} {
		if err = db.Create(&v).Error; err != nil {
			t.Fatal(err)
		}
	}
	bucket := "multirag-after67-a-" + suffix
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
		journal.Event("cleanup-bucket", map[string]any{"endpoint": cfg.Minio.Host, "bucket": bucket, "absent": !present})
	})
	cfg.Minio.Bucket = bucket
	cfg.Minio.PrefixPath = ""
	concrete, err := storage.NewMinioStorage(&cfg.Minio)
	if err != nil {
		t.Fatal(err)
	}
	store := &agentCaseStore{Storage: concrete, strict: concrete, journal: journal, bucket: bucket, keys: map[string]bool{}}
	previousStore := storage.GetStorageFactory().GetStorage()
	storage.GetStorageFactory().SetStorage(store)
	t.Cleanup(func() { storage.GetStorageFactory().SetStorage(previousStore) })
	gin.SetMode(gin.TestMode)
	engine := gin.New()
	engine.Use(func(c *gin.Context) {
		if id := c.GetHeader("X-Test-Principal"); id != "" {
			c.Set("user", &entity.User{ID: id})
		}
		c.Next()
	})
	h := handler.NewAgentHandler(service.NewAgentService())
	engine.GET("/api/v1/agents/:agent_id/versions", h.ListAgentVersions)
	engine.GET("/api/v1/agents/:agent_id/versions/:version_id", h.GetAgentVersion)
	engine.POST("/api/v1/agents/:agent_id/upload", h.UploadAgentFile)
	address := "127.0.0.1:18876"
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
	request := func(method, path, actor string, body io.Reader, contentType string) map[string]any {
		t.Helper()
		req, err := http.NewRequest(method, httpServer.URL+path, body)
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("X-Test-Principal", actor)
		if contentType != "" {
			req.Header.Set("Content-Type", contentType)
		}
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		var value map[string]any
		if err = json.NewDecoder(res.Body).Decode(&value); err != nil {
			t.Fatal(err)
		}
		if res.StatusCode != 200 {
			t.Fatal("unexpected HTTP status", res.StatusCode)
		}
		return value
	}
	check := func(value map[string]any, code common.ErrorCode) {
		t.Helper()
		if value["code"] != float64(code) {
			t.Fatalf("code want %d got %v", code, value)
		}
	}
	listPath := "/api/v1/agents/shared/versions"
	for _, actor := range []string{"owner", "normal", "admin"} {
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
	snapshot := func() string {
		t.Helper()
		var value string
		if err := read.QueryRow(`SELECT json_agg(row_to_json(v) ORDER BY v.id)::text FROM t_ai_user_canvas_version v`).Scan(&value); err != nil {
			t.Fatal(err)
		}
		return value
	}
	beforeSQL := snapshot()
	for _, entry := range []struct{ actor, canvas string }{{"foreign", "shared"}, {"invite", "shared"}, {"revoked", "shared"}, {"disabled", "shared"}, {"normal", "private"}, {"owner", "disabled-canvas"}, {"owner", "missing"}} {
		check(request("GET", "/api/v1/agents/"+entry.canvas+"/versions", entry.actor, nil, ""), common.CodeOperatingError)
		check(request("POST", "/api/v1/agents/"+entry.canvas+"/upload", entry.actor, strings.NewReader("malformed"), "multipart/form-data; boundary=x"), common.CodeOperatingError)
	}
	check(request("GET", listPath, "", nil, ""), common.CodeUnauthorized)
	if countObjects() != 0 || snapshot() != beforeSQL {
		t.Fatal("denial had side effects")
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
	for _, actor := range []string{"owner", "normal"} {
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
			key := actor + "-downloads/" + id
			if d["created_by"] != actor || d["preview_url"] != nil {
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
			if err != nil || persisted["created_by"] != actor || persisted["id"] != id {
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
	t.Setenv("MAX_FILE_NUM_PER_USER", "1")
	quotaBefore := countObjects()
	quotaBody, quotaCT := multipartBody([]string{"over quota"})
	check(request("POST", "/api/v1/agents/shared/upload", "owner", quotaBody, quotaCT), common.CodeOperatingError)
	if countObjects() != quotaBefore {
		t.Fatal("quota rejection wrote objects")
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
	store.mu.Lock()
	store.failAt = store.puts + 4
	store.mu.Unlock()
	body, ct = multipartBody([]string{"first", "second"})
	check(request("POST", "/api/v1/agents/shared/upload", "normal", body, ct), common.CodeServerError)
	if countObjects() != baseline {
		t.Fatal("failed batch left object keys")
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
		body, ct = multipartBody([]string{"must not persist"})
		check(request("POST", "/api/v1/agents/shared/upload"+query, "owner", body, ct), common.CodeArgumentError)
		if countObjects() != baseline {
			t.Fatal("raw query rejection wrote objects", query)
		}
	}
	var fileCount int
	if err = read.QueryRow("SELECT count(*) FROM t_ai_files").Scan(&fileCount); err != nil || fileCount != 0 {
		t.Fatal("runtime upload entered File tree", err)
	}
	journal.Event("acceptance-readback", map[string]any{"database": name, "version_count": 3, "ordinary_file_rows": fileCount, "object_count": baseline, "failed_batch_residue": 0, "denial_side_effects": 0, "http_principal": "controlled injected principal; real auth middleware deferred to I", "store": "real MinIO"})
	t.Log("real HTTP+PostgreSQL+MinIO: owner/normal/admin, denial matrix, stable summaries/release, detail binding, repeated/single uploads, persisted-write compensation, independent SQL/blob/descriptor readback")
}
