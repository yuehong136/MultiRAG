package handler

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v4"
	"github.com/google/uuid"
	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	redis "github.com/redis/go-redis/v9"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/cache"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/storage"
	"multirag/internal/testsupport"
)

type documentHTTPSecretStore struct{ secret string }

func (s documentHTTPSecretStore) Get(string) (string, error)             { return s.secret, nil }
func (documentHTTPSecretStore) Set(string, string, time.Duration) bool   { return true }
func (documentHTTPSecretStore) SetNX(string, string, time.Duration) bool { return true }

// This explicit opt-in test crosses real TCP, SQL, MinIO and Redis boundaries,
// then invokes the current independent Web consumer against the same server.
func TestAfterAabDocumentFilesHTTPAndWeb(t *testing.T) {
	configPath := os.Getenv("MULTIRAG_AFTERAAB_F_CONFIG")
	if configPath == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned F config required")
		}
		t.Skip("owned F config required")
	}
	journal := testsupport.NewCase(t)
	t.Chdir("../..")
	if err := common.Init("error"); err != nil {
		t.Fatal("test logger unavailable")
	}
	readConfig := func(path string, target any) {
		t.Helper()
		raw, err := os.ReadFile(path)
		if err != nil || json.Unmarshal(raw, target) != nil {
			t.Fatal("private live config unavailable")
		}
	}
	var cfg struct {
		Host, User, Password, Database string
		Port                           int
	}
	readConfig(configPath, &cfg)
	if cfg.Host != "127.0.0.1" || cfg.Port <= 0 || !strings.HasPrefix(cfg.Database, "multirag_afteraab_f_") {
		t.Fatal("owned TCP F database required")
	}
	var external struct {
		MinIO server.MinioConfig
		Redis struct {
			Host, Username, Password string
			DB                       int
		}
	}
	readConfig(filepath.Join(filepath.Dir(configPath), "external-private.json"), &external)
	endpoint := url.URL{Scheme: "postgres", User: url.UserPassword(cfg.User, cfg.Password), Host: net.JoinHostPort(cfg.Host, strconv.Itoa(cfg.Port)), Path: "/" + cfg.Database, RawQuery: "sslmode=disable&search_path=public"}
	journal.Event("reserve-HTTP-SQL-connections", map[string]any{"host": cfg.Host, "port": cfg.Port, "database": cfg.Database, "schema": "public"})
	db, err := gorm.Open(postgres.Open(endpoint.String()), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("HTTP SQL connection failed")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal("SQL pool unavailable")
	}
	t.Cleanup(func() { _ = pool.Close() })
	read, err := sql.Open("pgx", endpoint.String())
	if err != nil {
		t.Fatal("SQL independent read unavailable")
	}
	t.Cleanup(func() { _ = read.Close() })
	oldDB := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = oldDB })
	ids := map[string]string{}
	for _, name := range []string{"owner", "member", "foreign", "token-tenant", "member-owner", "member-personal", "foreign-owner", "owner-token", "member-token", "kb", "foreign-kb", "doc", "empty", "foreign-doc", "folder", "file", "empty-file", "link", "empty-link", "run", "artifact"} {
		ids[name] = strings.ReplaceAll(uuid.NewString(), "-", "")
	}
	apiToken := "afteraab-f-http-" + uuid.NewString()
	journal.Event("reserve-HTTP-fixture-identities", map[string]any{"sql_ids": ids, "api_token_sha256": fmt.Sprintf("%x", sha256.Sum256([]byte(apiToken)))})
	models := []any{&entity.APIToken{}, &entity.File2Document{}, &entity.File{}, &entity.Document{}, &entity.Knowledgebase{}, &entity.UserTenant{}, &entity.User{}, &entity.Tenant{}}
	allIDs := []string{}
	for _, id := range ids {
		allIDs = append(allIDs, id)
	}
	t.Cleanup(func() {
		if err := db.Where("token=?", apiToken).Delete(&entity.APIToken{}).Error; err != nil {
			t.Error("HTTP token cleanup failed")
		}
		for _, model := range models[1:] {
			if err := db.Where("id IN ?", allIDs).Delete(model).Error; err != nil {
				t.Error("HTTP owned SQL cleanup failed")
			}
		}
		remaining := map[string]int{}
		for _, table := range []string{"t_ai_file2documents", "t_ai_files", "t_ai_documents", "t_ai_knowledgebases", "t_ai_user_tenants", "t_ai_users", "t_ai_tenants"} {
			var n int
			if err := read.QueryRow("SELECT count(*) FROM "+table+" WHERE id=ANY($1)", allIDs).Scan(&n); err != nil || n != 0 {
				t.Error("HTTP independent SQL cleanup failed", table, n)
			}
			remaining[table] = n
		}
		var tokens int
		if err := read.QueryRow("SELECT count(*) FROM t_ai_api_tokens WHERE token=$1", apiToken).Scan(&tokens); err != nil || tokens != 0 {
			t.Error("HTTP token independent cleanup failed")
		}
		journal.Event("HTTP-independent-SQL-cleanup-readback", map[string]any{"remaining_owned_rows": remaining, "remaining_tokens": tokens})
	})
	one := func(s string) *string { return &s }
	users := []entity.User{}
	tenants := []entity.Tenant{}
	for _, name := range []string{"owner", "member", "foreign"} {
		users = append(users, entity.User{ID: ids[name], Nickname: name, Email: ids[name] + "@afteraab-f-http.test", Status: one("1"), IsActive: true, IsAuthenticated: true})
		tenants = append(tenants, entity.Tenant{ID: ids[name], Status: one("1"), ParserIDs: "naive"})
	}
	tenants = append(tenants, entity.Tenant{ID: ids["token-tenant"], Status: one("1"), ParserIDs: "naive"})
	members := []entity.UserTenant{}
	for _, row := range []struct{ id, user, tenant, role string }{{"member-owner", "owner", "owner", "owner"}, {"member-personal", "member", "member", "owner"}, {"foreign-owner", "foreign", "foreign", "owner"}, {"owner-token", "owner", "token-tenant", "owner"}, {"member-token", "member", "token-tenant", "normal"}} {
		members = append(members, entity.UserTenant{ID: ids[row.id], UserID: ids[row.user], TenantID: ids[row.tenant], Role: row.role, Status: one("1"), InvitedBy: ids[row.tenant]})
	}
	kbs := []entity.Knowledgebase{{ID: ids["kb"], TenantID: ids["token-tenant"], CreatedBy: ids["owner"], Name: "HTTP owned", Permission: "team", Status: one("1"), ParserConfig: entity.JSONMap{}}, {ID: ids["foreign-kb"], TenantID: ids["foreign"], CreatedBy: ids["foreign"], Name: "HTTP foreign", Permission: "me", Status: one("1"), ParserConfig: entity.JSONMap{}}}
	docs := []entity.Document{}
	for _, row := range []struct{ id, kb, name string }{{"doc", "kb", "源文件.json"}, {"empty", "kb", "empty.txt"}, {"foreign-doc", "foreign-kb", "foreign.json"}} {
		docs = append(docs, entity.Document{ID: ids[row.id], KbID: ids[row.kb], CreatedBy: ids["owner"], ParserID: "naive", ParserConfig: entity.JSONMap{}, Type: "doc", SourceType: "local", Name: one(row.name), Location: one("source.json"), Status: one("1")})
	}
	files := []entity.File{{ID: ids["folder"], TenantID: ids["token-tenant"], ParentID: ids["token-tenant"], CreatedBy: ids["owner"], Name: "HTTP owned folder", Type: "folder"}, {ID: ids["file"], TenantID: ids["token-tenant"], ParentID: ids["folder"], CreatedBy: ids["owner"], Name: "source.json", Type: "doc", SourceType: "local", Location: one("source.json")}, {ID: ids["empty-file"], TenantID: ids["token-tenant"], ParentID: ids["folder"], CreatedBy: ids["owner"], Name: "empty.txt", Type: "doc", SourceType: "local", Location: one("empty.txt")}}
	links := []entity.File2Document{{ID: ids["link"], FileID: one(ids["file"]), DocumentID: one(ids["doc"])}, {ID: ids["empty-link"], FileID: one(ids["empty-file"]), DocumentID: one(ids["empty"])}}
	for _, rows := range []any{&users, &tenants, &members, &kbs, &docs, &files, &links, &entity.APIToken{TenantID: ids["token-tenant"], Token: apiToken}} {
		if err := db.Create(rows).Error; err != nil {
			t.Fatal("HTTP owned SQL seed failed")
		}
	}
	var tokenUserCount int
	if err := read.QueryRow("SELECT count(*) FROM t_ai_users WHERE id=$1", ids["token-tenant"]).Scan(&tokenUserCount); err != nil || tokenUserCount != 0 {
		t.Fatal("API token fixture must retain tenant identity independent of a user")
	}

	ctx := context.Background()
	const bucket = "afteraab-f-http-20261029"
	artifactFilename := ids["artifact"] + ".json"
	objects := map[string][]byte{ids["folder"] + "/source.json": []byte(`{"code":109,"message":"source data","data":null}`), ids["folder"] + "/empty.txt": {}, "sandbox-artifacts/" + artifactFilename: []byte(`{"code":109,"message":"artifact source","data":null}`)}
	objectInputs := map[string]any{}
	for key, data := range objects {
		objectInputs[key] = map[string]any{"size": len(data), "sha256": fmt.Sprintf("%x", sha256.Sum256(data))}
	}
	journal.Event("reserve-HTTP-MinIO-before-create", map[string]any{"endpoint": external.MinIO.Host, "bucket": bucket, "objects": objectInputs})
	minioClient, err := minio.New(external.MinIO.Host, &minio.Options{Creds: credentials.NewStaticV4(external.MinIO.User, external.MinIO.Password, ""), Secure: external.MinIO.Secure, Region: external.MinIO.Region})
	if err != nil {
		t.Fatal("MinIO client unavailable")
	}
	exists, err := minioClient.BucketExists(ctx, bucket)
	if err != nil || exists {
		t.Fatal("HTTP MinIO bucket must be absent before creation")
	}
	t.Cleanup(func() {
		for key := range objects {
			if err := minioClient.RemoveObject(context.Background(), bucket, key, minio.RemoveObjectOptions{}); err != nil {
				t.Error("HTTP object cleanup failed")
			}
		}
		if err := minioClient.RemoveBucket(context.Background(), bucket); err != nil {
			t.Error("HTTP bucket cleanup failed")
		}
		other, err := minio.New(external.MinIO.Host, &minio.Options{Creds: credentials.NewStaticV4(external.MinIO.User, external.MinIO.Password, ""), Secure: external.MinIO.Secure, Region: external.MinIO.Region})
		if err != nil {
			t.Error("HTTP independent MinIO client failed")
			return
		}
		exists, err := other.BucketExists(context.Background(), bucket)
		if err != nil || exists {
			t.Error("HTTP independent bucket cleanup readback failed")
		}
		journal.Event("HTTP-independent-MinIO-cleanup-readback", map[string]any{"bucket": bucket, "absent": !exists, "borrowed_service_preserved": true})
	})
	if err := minioClient.MakeBucket(ctx, bucket, minio.MakeBucketOptions{Region: external.MinIO.Region}); err != nil {
		t.Fatal("HTTP MinIO bucket create failed")
	}
	for key, data := range objects {
		if _, err := minioClient.PutObject(ctx, bucket, key, bytes.NewReader(data), int64(len(data)), minio.PutObjectOptions{}); err != nil {
			t.Fatal("HTTP source object upload failed")
		}
	}
	minioCfg := external.MinIO
	minioCfg.Bucket = bucket
	minioCfg.PrefixPath = ""
	store, err := storage.NewMinioStorage(&minioCfg)
	if err != nil {
		t.Fatal("HTTP MinIO storage unavailable")
	}
	factory := storage.GetStorageFactory()
	oldStore := factory.GetStorage()
	factory.SetStorage(store)
	t.Cleanup(func() { factory.SetStorage(oldStore) })
	t.Setenv("SANDBOX_ARTIFACT_BUCKET", "sandbox-artifacts")

	redisHost, redisPort, err := net.SplitHostPort(external.Redis.Host)
	if err != nil || external.Redis.Username != "" {
		t.Fatal("explicit Redis host:port with default user required")
	}
	port, err := strconv.Atoi(redisPort)
	if err != nil {
		t.Fatal("Redis port invalid")
	}
	redisKey := "multirag:sandbox_artifact:" + artifactFilename
	binding, _ := json.Marshal(map[string]any{"owner_id": ids["owner"], "run_id": ids["run"], "session_id": nil})
	journal.Event("reserve-HTTP-Redis-binding-before-write", map[string]any{"endpoint": external.Redis.Host, "database": external.Redis.DB, "key": redisKey, "owner": ids["owner"], "run": ids["run"], "session": nil, "binding_sha256": fmt.Sprintf("%x", sha256.Sum256(binding))})
	if cache.Get() != nil {
		t.Fatal("HTTP live test requires uninitialized private Redis client")
	}
	if err := cache.Init(&server.RedisConfig{Host: redisHost, Port: port, Password: external.Redis.Password, DB: external.Redis.DB}); err != nil {
		t.Fatal("HTTP Redis connection failed")
	}
	t.Cleanup(func() { _ = cache.Close() })
	rc := cache.Get().GetClient()
	existing, err := rc.Exists(ctx, redisKey).Result()
	if err != nil || existing != 0 {
		t.Fatal("HTTP artifact Redis key must not preexist")
	}
	t.Cleanup(func() {
		cache.Get().DeleteIfEqual(redisKey, string(binding))
		check := redis.NewClient(&redis.Options{Addr: external.Redis.Host, Username: external.Redis.Username, Password: external.Redis.Password, DB: external.Redis.DB})
		defer check.Close()
		n, err := check.Exists(context.Background(), redisKey).Result()
		if err != nil || n != 0 {
			t.Error("HTTP independent Redis cleanup readback failed")
		}
		journal.Event("HTTP-independent-Redis-cleanup-readback", map[string]any{"key": redisKey, "remaining": n})
	})
	created, err := rc.SetNX(ctx, redisKey, string(binding), 5*time.Minute).Result()
	if err != nil || !created {
		t.Fatal("HTTP artifact Redis binding create failed")
	}

	secret := strings.Repeat(ids["run"], 2)
	oldSecret := server.GetSecretKey()
	if err := server.InitVariables(documentHTTPSecretStore{secret: secret}); err != nil {
		t.Fatal("HTTP signing store init failed")
	}
	server.SetSecretKey(secret)
	t.Cleanup(func() { server.SetSecretKey(oldSecret) })
	status := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned F HTTP test")
	t.Cleanup(func() { local.SetAdminStatus(status.Status, status.Reason) })
	jwtFor := func(name string) string {
		t.Helper()
		token, err := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.RegisteredClaims{Subject: ids[name] + "@afteraab-f-http.test", ExpiresAt: jwt.NewNumericDate(time.Now().Add(5 * time.Minute))}).SignedString([]byte(secret))
		if err != nil {
			t.Fatal("HTTP JWT signing failed")
		}
		return token
	}
	ownerJWT, memberJWT := jwtFor("owner"), jwtFor("member")
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(gin.Recovery())
	dh := &DocumentHandler{}
	ah := NewAuthHandler()
	web := r.Group("/api/v1")
	web.Use(func(c *gin.Context) { documentFileHeaders(c) }, ah.DocumentStatusAuthMiddleware())
	web.GET("/documents/:id/preview", dh.GetDocumentPreview)
	web.GET("/documents/:id/download", dh.DownloadDocumentFile)
	web.GET("/documents/artifact/:filename", dh.GetDocumentArtifact)
	r.GET("/api/v1/datasets/:dataset_id/documents/:document_id", ah.DatasetDocumentAuthMiddleware(), dh.DownloadDocument)
	journal.Event("reserve-HTTP-listener-before-bind", map[string]any{"network": "tcp4", "requested": "127.0.0.1:0"})
	lc := net.ListenConfig{Control: func(network, address string, _ syscall.RawConn) error {
		journal.Event("HTTP-listener-Control-before-bind", map[string]any{"network": network, "address": address})
		return nil
	}}
	listener, err := lc.Listen(ctx, "tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal("owned HTTP listener bind failed")
	}
	address := listener.Addr().String()
	journal.Event("HTTP-listener-bound", map[string]any{"address": address})
	httpServer := &http.Server{Handler: r, ReadHeaderTimeout: 5 * time.Second}
	done := make(chan error, 1)
	go func() { done <- httpServer.Serve(listener) }()
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		if err := httpServer.Shutdown(ctx); err != nil {
			t.Error("owned HTTP shutdown failed")
		}
		if err := <-done; err != http.ErrServerClosed {
			t.Error("owned HTTP serve exit failed")
		}
		connection, err := net.DialTimeout("tcp4", address, 200*time.Millisecond)
		if err == nil {
			_ = connection.Close()
			t.Error("owned HTTP listener still bound")
		}
		journal.Event("HTTP-listener-cleanup-readback", map[string]any{"address": address, "connect_refused": err != nil})
	})
	base := "http://" + address
	client := &http.Client{Timeout: 5 * time.Second}
	defer client.CloseIdleConnections()
	get := func(path, token string, want int, wantData []byte) {
		t.Helper()
		req, _ := http.NewRequest(http.MethodGet, base+path, nil)
		if token != "" {
			req.Header.Set("Authorization", "Bearer "+token)
		}
		response, err := client.Do(req)
		if err != nil {
			t.Fatal("real HTTP request failed")
		}
		defer response.Body.Close()
		data, err := io.ReadAll(response.Body)
		if err != nil || response.StatusCode != want {
			t.Fatalf("HTTP contract failed path=%s status=%d", path, response.StatusCode)
		}
		if response.Header.Get("Cache-Control") != "private, no-store" || response.Header.Get("X-Content-Type-Options") != "nosniff" {
			t.Fatal("HTTP binary privacy headers missing")
		}
		if wantData != nil && !bytes.Equal(data, wantData) {
			t.Fatal("HTTP binary source bytes changed")
		}
	}
	preview := "/api/v1/documents/" + ids["doc"] + "/preview"
	download := "/api/v1/documents/" + ids["doc"] + "/download"
	artifact := "/api/v1/documents/artifact/" + artifactFilename + "?run_id=" + ids["run"]
	get(preview, ownerJWT, 200, objects[ids["folder"]+"/source.json"])
	get(preview, memberJWT, 200, objects[ids["folder"]+"/source.json"])
	get(download, memberJWT, 403, nil)
	get(download, ownerJWT, 200, objects[ids["folder"]+"/source.json"])
	get("/api/v1/documents/"+ids["empty"]+"/download", ownerJWT, 200, []byte{})
	get(artifact, ownerJWT, 200, objects["sandbox-artifacts/"+artifactFilename])
	get(artifact, memberJWT, 404, nil)
	get(preview, "", 401, nil)
	get("/api/v1/datasets/"+ids["kb"]+"/documents/"+ids["doc"], apiToken, 200, objects[ids["folder"]+"/source.json"])
	get("/api/v1/datasets/"+ids["foreign-kb"]+"/documents/"+ids["doc"], apiToken, 404, nil)
	journal.Event("real-HTTP-SQL-MinIO-Redis-contract-pass", map[string]any{"owner_download": true, "member_preview": true, "member_download_status": 403, "foreign_artifact_status": 404, "empty_object_status": 200, "SDK_original_token_tenant": true, "token_tenant_has_user_row": false})
	runDocumentHTTPWebConsumer(t, journal, map[string]any{"base": base, "ownerJWT": ownerJWT, "memberJWT": memberJWT, "doc": ids["doc"], "empty": ids["empty"], "artifact": artifact, "source": string(objects[ids["folder"]+"/source.json"]), "artifactSource": string(objects["sandbox-artifacts/"+artifactFilename])})
}

func runDocumentHTTPWebConsumer(t *testing.T, journal *testsupport.CaseJournal, fixture map[string]any) {
	t.Helper()
	webRoot := "/Users/xldu/project/web"
	if value := os.Getenv("MULTIRAG_AFTERAAB_F_WEB_ROOT"); value != "" {
		webRoot = value
	}
	entry := filepath.Join(os.TempDir(), "afteraab-f-web-"+uuid.NewString()+".mts")
	fixturePath := strings.TrimSuffix(entry, ".mts") + ".json"
	// Dynamic imports run only after browser storage and transport are configured.
	script := strings.ReplaceAll(documentHTTPWebScript, "__WEB_ROOT__", filepath.ToSlash(webRoot))
	raw, err := json.Marshal(fixture)
	if err != nil {
		t.Fatal("Web fixture marshal failed")
	}
	journal.Event("reserve-Web-consumer-temp-inputs-before-write", map[string]any{"script": entry, "fixture": fixturePath, "script_sha256": fmt.Sprintf("%x", sha256.Sum256([]byte(script))), "fixture_sha256": fmt.Sprintf("%x", sha256.Sum256(raw)), "borrowed_web_root": webRoot, "borrowed_tsx": filepath.Join(webRoot, "node_modules/.bin/tsx"), "borrowed_tsconfig": filepath.Join(webRoot, "tsconfig.app.json")})
	t.Cleanup(func() {
		for _, path := range []string{entry, fixturePath} {
			if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
				t.Error("Web private temp cleanup failed")
			}
			if _, err := os.Stat(path); !os.IsNotExist(err) {
				t.Error("Web temp cleanup readback failed")
			}
		}
		journal.Event("Web-temp-cleanup-readback", map[string]any{"script_absent": true, "private_fixture_absent": true})
	})
	if err := os.WriteFile(entry, []byte(script), 0600); err != nil {
		t.Fatal("Web harness write failed")
	}
	if err := os.WriteFile(fixturePath, raw, 0600); err != nil {
		t.Fatal("Web private fixture write failed")
	}
	node, err := exec.LookPath("node")
	if err != nil {
		t.Fatal("existing Node runtime required for Web consumer")
	}
	register := filepath.Join(webRoot, "node_modules/tsx/dist/esm/index.mjs")
	journal.Event("reserve-Web-consumer-process-before-spawn", map[string]any{"node": node, "tsx_register": register, "script": entry, "private_fixture": fixturePath, "same_runner_process_group": true})
	processContext, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	cmd := exec.CommandContext(processContext, node, "--import", register, entry, fixturePath)
	cmd.Dir = webRoot
	cmd.Env = append(os.Environ(), "TSX_TSCONFIG_PATH="+filepath.Join(webRoot, "tsconfig.app.json"))
	output, err := cmd.CombinedOutput()
	exitCode := -1
	if cmd.ProcessState != nil {
		exitCode = cmd.ProcessState.ExitCode()
	}
	journal.Event("Web-consumer-command-exit", map[string]any{"command": "existing Web tsx document-files/artifact-resource actual modules", "raw_exit": exitCode, "output": string(output)})
	if err != nil {
		t.Fatalf("actual Web consumer failed: %s", output)
	}
	t.Log(strings.TrimSpace(string(output)))
}

const documentHTTPWebScript = `
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
const f = JSON.parse(readFileSync(process.argv[2], 'utf8'))
const values = new Map([['auth_token', f.ownerJWT]])
Object.assign(globalThis, { localStorage: {getItem:(k)=>values.get(k)??null,setItem:(k,v)=>values.set(k,String(v)),removeItem:(k)=>values.delete(k)} })
const blobs: Blob[] = []
const links: any[] = []
Object.assign(globalThis, { window: {URL:{createObjectURL:(b)=>{blobs.push(b);return 'blob:owned'},revokeObjectURL:()=>{}},location:{href:'http://localhost:8000/home'},history:{replaceState:()=>{}},dispatchEvent:()=>{}},document:{createElement:()=>({click(){links.push({...this})}}),body:{appendChild:()=>{},removeChild:()=>{}}} })
const originalFetch = globalThis.fetch
const paths:string[] = []
globalThis.fetch = async (input:any, init:any) => {
  const logical = new URL(typeof input === 'string' ? input : input.url ?? String(input))
  assert.equal(logical.origin, 'http://localhost:8000')
  paths.push(logical.pathname)
  return originalFetch(f.base+logical.pathname+logical.search, init)
}
const {getDocumentFileUrl,downloadDocumentFile}=await import('__WEB_ROOT__/src/api/document-files.ts')
const {apiClient}=await import('__WEB_ROOT__/src/api/client.ts')
const {fetchPreviewResource}=await import('__WEB_ROOT__/src/lib/knowledge/preview-resource.ts')
const {fetchArtifactBlob}=await import('__WEB_ROOT__/src/components/chat/artifact-resource.ts')
apiClient.setAuthToken(f.ownerJWT)
const preview=await fetchPreviewResource(getDocumentFileUrl(f.doc,'preview'))
assert.equal(preview.status,200);assert.equal(await preview.text(),f.source)
await downloadDocumentFile(f.doc)
assert.equal(await blobs.at(-1)!.text(),f.source)
assert.equal(links.at(-1).download,'源文件.json')
await downloadDocumentFile(f.empty)
assert.equal(blobs.at(-1)!.size,0)
const artifact=await fetchArtifactBlob('http://localhost:8000'+f.artifact)
assert.equal(await artifact.text(),f.artifactSource)
apiClient.setAuthToken(f.memberJWT)
const before=blobs.length
await assert.rejects(downloadDocumentFile(f.doc),(error:any)=>error.status===403)
assert.equal(blobs.length,before)
await assert.rejects(fetchArtifactBlob('http://localhost:8000'+f.artifact))
assert.ok(paths.some((p)=>p.endsWith('/preview')))
assert.ok(paths.some((p)=>p.endsWith('/download')))
assert.ok(paths.some((p)=>p.includes('/artifact/')))
console.log(JSON.stringify({consumer:'current Web document-files and artifact-resource',HTTPRequests:paths.length,preview:true,download:true,emptyDownload:true,artifactJSONSource:true,memberDownloadDenied:true,foreignArtifactDenied:true}))
`
