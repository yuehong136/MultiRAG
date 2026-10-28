package router

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"image"
	"image/png"
	"io"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/engine"
	"multirag/internal/engine/types"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/storage"
	"multirag/internal/testsupport"
	"multirag/internal/utility"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type after53ImageStore struct {
	storage.Storage
	reads atomic.Int64
	fail  bool
}

func (s *after53ImageStore) Get(bucket, key string, tenant ...string) ([]byte, error) {
	s.reads.Add(1)
	if s.fail {
		return nil, fmt.Errorf("controlled storage failure")
	}
	return s.Storage.Get(bucket, key, tenant...)
}

type after53ImageIndex struct {
	engine.DocEngine
	rows  []map[string]interface{}
	calls int
	fail  bool
}

func (s *after53ImageIndex) Search(ctx context.Context, req *types.SearchRequest) (*types.SearchResult, error) {
	s.calls++
	if len(req.KbIDs) != 1 || len(req.IndexNames) != 1 || !req.IncludeUnavailable || req.Filter["img_id"] == nil || req.Limit != 100 {
		return nil, fmt.Errorf("unscoped index query")
	}
	if s.fail {
		return nil, fmt.Errorf("controlled index failure")
	}
	return &types.SearchResult{Chunks: s.rows}, nil
}

// TestAfter53GHTTPAndSQL runs real JWT+TCP HTTP against scratch SQL and MinIO.
func TestAfter53GHTTPAndSQL(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_AFTER53_G_DSN")
	if dsn == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned scratch DB required")
		}
		t.Skip("requires owned scratch DB and MinIO")
	}
	u, err := url.Parse(dsn)
	if err != nil || !strings.HasPrefix(strings.TrimPrefix(u.Path, "/"), "multirag_after53_g_") {
		t.Fatal("unowned DB")
	}
	j := testsupport.NewCase(t)
	ctx := context.Background()
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	cfgPath := filepath.Join(os.Getenv("TMPDIR"), "after53-g.yaml")
	j.Event("reserve-config", map[string]any{"path": cfgPath})
	if err := os.WriteFile(cfgPath, []byte("multirag:\n  secret_key: after53-g-owned-jwt-key-32-characters\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(cfgPath); err != nil {
		t.Fatal(err)
	}
	if err := server.InitVariables(nil); err != nil {
		t.Fatal(err)
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("scratch connect")
	}
	pool, _ := db.DB()
	t.Cleanup(func() { pool.Close() })
	reader, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("reader connect")
	}
	t.Cleanup(func() { reader.Close() })
	prev := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = prev })
	oldAdmin := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned test")
	t.Cleanup(func() { local.SetAdminStatus(oldAdmin.Status, oldAdmin.Reason) })
	j.Event("reserve-sql-schema", map[string]any{"database": strings.TrimPrefix(u.Path, "/"), "tables": []string{"t_ai_users", "t_ai_user_tenants", "t_ai_api_tokens", "t_ai_knowledgebases", "t_ai_documents", "t_pipeline_operation_log"}})
	if err := db.AutoMigrate(&entity.User{}, &entity.UserTenant{}, &entity.APIToken{}, &entity.Knowledgebase{}, &entity.Document{}, &entity.PipelineOperationLog{}); err != nil {
		t.Fatal(err)
	}
	kb := strings.ReplaceAll(uuid.NewString(), "-", "")
	foreignKB := strings.ReplaceAll(uuid.NewString(), "-", "")
	j.Event("reserve-sql-identities", map[string]any{"users": []string{"owner", "member", "foreign", "revoked"}, "datasets": []string{kb, foreignKB}, "documents": []string{"doc", "empty", "nil", "inline", "foreign-doc"}, "logs": []string{"log-0", "log-1", "log-2", "file", "forged-tenant", "foreign-log"}, "tokenNames": []string{"missing", "empty", "preserved", "foreign"}})
	active, inactive, session := "1", "0", "owned-session"
	for _, id := range []string{"owner", "member", "foreign", "revoked"} {
		if err := db.Create(&entity.User{ID: id, Email: id + "@after53.invalid", Nickname: id, AccessToken: &session, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		tenant, status := "owner", active
		if id == "foreign" {
			tenant = id
		}
		if id == "revoked" {
			status = inactive
		}
		role := "normal"
		if id == "owner" {
			role = "owner"
		}
		if err := db.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: tenant, Role: role, InvitedBy: "owner", Status: &status}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for _, id := range []string{kb, foreignKB} {
		tenant := "owner"
		if id == foreignKB {
			tenant = "foreign"
		}
		if err := db.Create(&entity.Knowledgebase{ID: id, Name: id, TenantID: tenant, CreatedBy: tenant, Permission: "team", EmbdID: "none", ParserConfig: entity.JSONMap{}, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	empty, preserved := "", "existing-beta"
	for _, row := range []entity.APIToken{{TenantID: "owner", Token: "missing"}, {TenantID: "owner", Token: "empty", Beta: &empty}, {TenantID: "owner", Token: "preserved", Beta: &preserved}, {TenantID: "foreign", Token: "missing"}} {
		if err := db.Create(&row).Error; err != nil {
			t.Fatal(err)
		}
	}
	key := "folder/a-b image.png"
	inline := "data:image/jpeg;base64,owned"
	for _, id := range []string{"doc", "empty", "nil", "inline", "foreign-doc"} {
		var thumb *string
		scope := kb
		run := "0"
		if id == "doc" {
			thumb = &key
		}
		if id == "empty" {
			thumb = &empty
		}
		if id == "inline" {
			thumb = &inline
		}
		if id == "foreign-doc" {
			scope = foreignKB
		}
		if err := db.Create(&entity.Document{ID: id, KbID: scope, ParserID: "naive", ParserConfig: entity.JSONMap{}, CreatedBy: "owner", Type: "txt", Suffix: "txt", Run: &run, Thumbnail: thumb, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	for i, id := range []string{"log-0", "log-1", "log-2", "file", "forged-tenant", "foreign-log"} {
		scope, tenant, doc := kb, "owner", "graph_raptor_x"
		if id == "file" {
			doc = "doc"
		}
		if id == "forged-tenant" {
			tenant = "foreign"
		}
		if id == "foreign-log" {
			scope = foreignKB
		}
		stamp := int64(17)
		date := time.Date(2026, 1, 2, 0, 0, 0, 0, time.UTC)
		row := entity.PipelineOperationLog{ID: id, TenantID: tenant, KbID: scope, DocumentID: doc, ParserID: "naive", DocumentName: "Report_100%报告", DocumentSuffix: "txt", DocumentType: "txt", SourceFrom: "local", OperationStatus: fmt.Sprint(i % 5), TaskType: "parse", DSL: entity.JSONMap{"components": map[string]any{}, "secret": "[REDACTED]"}, BaseModel: entity.BaseModel{CreateTime: &stamp, CreateDate: &date}}
		if err := db.Create(&row).Error; err != nil {
			t.Fatal(err)
		}
	}
	client, err := minio.New(os.Getenv("MULTIRAG_AFTER53_G_MINIO_HOST"), &minio.Options{Creds: credentials.NewStaticV4(os.Getenv("MULTIRAG_AFTER53_G_MINIO_USER"), os.Getenv("MULTIRAG_AFTER53_G_MINIO_PASSWORD"), ""), Secure: false})
	if err != nil {
		t.Fatal("minio client")
	}
	keys := []string{key, "chunk-image.png", "invalid.svg"}
	j.Event("reserve-objects", map[string]any{"bucket": kb, "keys": keys, "borrowed": "MinIO service"})
	t.Cleanup(func() {
		for _, key := range keys {
			if err := client.RemoveObject(ctx, kb, key, minio.RemoveObjectOptions{}); err != nil {
				t.Error("object removal failed")
			}
		}
		if err := client.RemoveBucket(ctx, kb); err != nil {
			t.Error("bucket removal failed")
		}
		exists, err := client.BucketExists(ctx, kb)
		if err != nil || exists {
			t.Error("bucket cleanup readback failed")
		}
		j.Event("object-cleanup-readback", map[string]any{"bucket": kb, "absent": !exists, "error": err != nil})
	})
	if err := client.MakeBucket(ctx, kb, minio.MakeBucketOptions{}); err != nil {
		t.Fatal("make owned bucket")
	}
	var pngBytes bytes.Buffer
	if err := png.Encode(&pngBytes, image.NewRGBA(image.Rect(0, 0, 2, 2))); err != nil {
		t.Fatal(err)
	}
	for _, k := range keys {
		data := pngBytes.Bytes()
		if k == "invalid.svg" {
			data = []byte("<svg></svg>")
		}
		if _, err := client.PutObject(ctx, kb, k, bytes.NewReader(data), int64(len(data)), minio.PutObjectOptions{}); err != nil {
			t.Fatal("put owned object")
		}
	}
	baseStore, err := storage.NewMinioStorage(&server.MinioConfig{Host: os.Getenv("MULTIRAG_AFTER53_G_MINIO_HOST"), User: os.Getenv("MULTIRAG_AFTER53_G_MINIO_USER"), Password: os.Getenv("MULTIRAG_AFTER53_G_MINIO_PASSWORD")})
	if err != nil {
		t.Fatal("storage init")
	}
	spy := &after53ImageStore{Storage: baseStore}
	factory := storage.GetStorageFactory()
	oldStore := factory.GetStorage()
	factory.SetStorage(spy)
	t.Cleanup(func() { factory.SetStorage(oldStore) })
	index := &after53ImageIndex{}
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	(&Router{authHandler: handler.NewAuthHandler(), documentHandler: handler.NewDocumentHandler(service.NewDocumentService(index)), datasetsHandler: handler.NewDatasetsHandler(service.NewDatasetsService()), systemHandler: handler.NewSystemHandler(service.NewSystemService())}).Setup(app)
	address := fmt.Sprintf("127.0.0.1:%d", 20000+int(time.Now().UnixNano()%40000))
	j.Event("reserve-listener", map[string]any{"address": address})
	listener, err := net.Listen("tcp", address)
	if err != nil {
		t.Fatal(err)
	}
	httpServer := &httptest.Server{Listener: listener, Config: &http.Server{Handler: app}}
	httpServer.Start()
	t.Cleanup(func() {
		httpServer.Close()
		probe, err := net.DialTimeout("tcp", address, time.Second)
		if err == nil {
			probe.Close()
			t.Error("listener remains")
		}
		j.Event("listener-cleanup-readback", map[string]any{"address": address, "absent": err != nil})
	})
	jwt := map[string]string{}
	for _, user := range []string{"owner", "member", "foreign", "revoked"} {
		token, err := utility.SignUserJWT(user+"@after53.invalid", server.GetSecretKey())
		if err != nil {
			t.Fatal(err)
		}
		jwt[user] = token
	}
	request := func(path, user string) (int, map[string]any, []byte, http.Header) {
		t.Helper()
		req, _ := http.NewRequest("GET", httpServer.URL+path, nil)
		if user != "" {
			req.Header.Set("Authorization", "Bearer "+jwt[user])
		}
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		raw, _ := io.ReadAll(res.Body)
		var out map[string]any
		_ = json.Unmarshal(raw, &out)
		return res.StatusCode, out, raw, res.Header
	}
	snapshot := func() string {
		t.Helper()
		var out string
		if err := reader.QueryRow(`SELECT jsonb_build_object('documents',(SELECT jsonb_agg(to_jsonb(d) ORDER BY id) FROM t_ai_documents d),'logs',(SELECT jsonb_agg(to_jsonb(l) ORDER BY id) FROM t_pipeline_operation_log l))::text`).Scan(&out); err != nil {
			t.Fatal(err)
		}
		return out
	}
	var logQueries atomic.Int64
	observer := "after53-g-log-read"
	if err := db.Callback().Query().Before("gorm:query").Register(observer, func(tx *gorm.DB) {
		if tx.Statement.Table == "t_pipeline_operation_log" {
			logQueries.Add(1)
		}
	}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Callback().Query().Remove(observer) })
	before := snapshot()
	_, out, _, _ := request("/api/v1/thumbnails?doc_ids=doc&doc_ids=empty&doc_ids=nil&doc_ids=inline&doc_ids=foreign-doc&doc_ids=doc", "member")
	if out["code"] != float64(0) {
		t.Fatal(out)
	}
	thumbs := out["data"].(map[string]any)
	if len(thumbs) != 4 || thumbs["nil"] != nil || thumbs["empty"] != "" || thumbs["inline"] != inline {
		t.Fatal(thumbs)
	}
	imagePath := "/api/v1/documents/images/" + kb + "-" + url.PathEscape(key)
	for _, user := range []string{"foreign", "revoked", ""} {
		reads := spy.reads.Load()
		_, out, _, _ := request(imagePath, user)
		if out["code"] == float64(0) || spy.reads.Load() != reads {
			t.Fatal("denial read object", user, out)
		}
	}
	status, _, raw, headers := request(imagePath, "member")
	if status != 200 || !bytes.Equal(raw, pngBytes.Bytes()) || headers.Get("Content-Type") != "image/png" || headers.Get("Cache-Control") != "no-store" {
		t.Fatal("image response mismatch", status, string(raw), headers)
	}
	for _, docID := range []string{"missing", "foreign-doc"} {
		index.rows = []map[string]interface{}{{"img_id": kb + "-chunk-image.png", "kb_id": kb, "doc_id": docID}}
		reads := spy.reads.Load()
		_, out, _, _ := request("/api/v1/documents/images/"+kb+"-chunk-image.png", "member")
		if out["code"] == float64(0) || spy.reads.Load() != reads {
			t.Fatal("unowned index registration read object")
		}
	}
	index.rows = []map[string]interface{}{{"img_id": kb + "-chunk-image.png", "kb_id": kb, "doc_id": "doc"}}
	status, _, raw, _ = request("/api/v1/documents/images/"+kb+"-chunk-image.png", "member")
	if status != 200 || !bytes.Equal(raw, pngBytes.Bytes()) {
		t.Fatal("registered chunk missing")
	}
	index.fail = true
	reads := spy.reads.Load()
	_, out, _, _ = request("/api/v1/documents/images/"+kb+"-unknown", "member")
	if out["code"] == float64(0) || spy.reads.Load() != reads {
		t.Fatal("index failure read object")
	}
	if out["code"] != float64(common.CodeServerError) {
		t.Fatal("index uncertainty classified as missing", out)
	}
	index.fail = false
	spy.fail = true
	_, out, _, _ = request(imagePath, "member")
	if out["code"] == float64(0) {
		t.Fatal("storage error succeeded")
	}
	if out["code"] != float64(common.CodeServerError) {
		t.Fatal("storage uncertainty classified as missing", out)
	}
	spy.fail = false
	index.rows = []map[string]interface{}{{"img_id": kb + "-invalid.svg", "kb_id": kb, "doc_id": "doc"}}
	_, out, _, _ = request("/api/v1/documents/images/"+kb+"-invalid.svg", "member")
	if out["code"] == float64(0) {
		t.Fatal("invalid raster succeeded")
	}
	path := "/api/v1/datasets/" + kb + "/ingestions"
	_, out, _, _ = request(path+"/summary", "member")
	if out["code"] != float64(0) || len(out["data"].(map[string]any)["status"].(map[string]any)) != 5 {
		t.Fatal(out)
	}
	seen := map[string]bool{}
	for page := 1; page <= 3; page++ {
		_, out, _, _ = request(path+fmt.Sprintf("?page=%d&page_size=1&desc=false", page), "member")
		if out["code"] != float64(0) {
			t.Fatal(out)
		}
		data := out["data"].(map[string]any)
		if data["total"] != float64(3) {
			t.Fatal("tenant/partition leak", data)
		}
		rows := data["logs"].([]any)
		id := rows[0].(map[string]any)["id"].(string)
		if seen[id] {
			t.Fatal("unstable tied page", id)
		}
		seen[id] = true
	}
	for query, want := range map[string]int{"log_type=file": 1, "keywords=": 3, "log_type=file&types=pdf": 0, "log_type=file&suffix=txt": 1, "operation_status=0": 1, "operation_status=UNSTART": 1, "keywords=REPORT_100%25%E6%8A%A5%E5%91%8A": 3, "keywords=REPORT_100_": 0, "page=0&page_size=1": 1, "create_date_from=2026-01-02T08%3A00%3A00%2B08%3A00&create_date_to=2026-01-02": 3, "create_date_from=2026-01-03": 0} {
		_, out, _, _ = request(path+"?"+query, "owner")
		if out["code"] != float64(0) {
			t.Fatal(query, out)
		}
		rows := out["data"].(map[string]any)["logs"].([]any)
		if len(rows) != want {
			t.Fatal(query, out)
		}
	}
	_, out, _, _ = request(path+"/file", "member")
	if out["code"] != float64(0) {
		t.Fatal(out)
	}
	detail := out["data"].(map[string]any)
	if len(detail) != 24 || detail["document_id"] != "doc" || detail["dsl"] == nil {
		t.Fatal("truncated detail", detail)
	}
	for _, suffix := range []string{"/foreign-log", "/forged-tenant"} {
		_, out, _, _ = request(path+suffix, "owner")
		if out["code"] == float64(0) {
			t.Fatal("foreign detail leaked")
		}
	}
	for _, user := range []string{"foreign", "revoked", ""} {
		_, out, _, _ = request(path, ""+user)
		if out["code"] == float64(0) {
			t.Fatal("foreign logs leaked")
		}
	}
	if logQueries.Load() == 0 {
		t.Fatal("log query observer did not observe successful queries")
	}
	for _, query := range []string{"page=x", "page=1&page=2", "page_size=101", "operation_status=", "create_date_from=bad", "orderby=dsl", "operation_status=%zz", "create_date_from=%zz", "operation_status=%zz&operation_status=0", "operation_status=0;operation_status=1", "create_date_from=2026-01-01;page=1"} {
		queriesBefore := logQueries.Load()
		_, out, _, _ = request(path+"?"+query, "owner")
		if out["code"] != float64(common.CodeArgumentError) {
			t.Fatal("malformed HTTP query accepted", query, out)
		}
		if logQueries.Load() != queriesBefore || snapshot() != before {
			t.Fatal("malformed query read logs or mutated SQL", query)
		}
	}
	if err := db.Exec("ALTER TABLE t_pipeline_operation_log RENAME TO owned_log_failure").Error; err != nil {
		t.Fatal(err)
	}
	_, out, _, _ = request(path, "owner")
	if out["code"] != float64(common.CodeServerError) {
		t.Fatal("SQL list error succeeded", out)
	}
	if err := db.Exec("ALTER TABLE owned_log_failure RENAME TO t_pipeline_operation_log").Error; err != nil {
		t.Fatal(err)
	}
	if snapshot() != before {
		t.Fatal("read or denial mutated state")
	}
	// Execute the checked-out Web API modules, preserving their real HTTP client.
	webRoot := os.Getenv("MULTIRAG_AFTER53_G_WEB_CHECKOUT")
	node := os.Getenv("MULTIRAG_AFTER53_G_NODE")
	if webRoot == "" || node == "" {
		t.Fatal("actual Web checkout and Node required")
	}
	script, err := filepath.Abs("testdata/after53_g_consumer.mts")
	if err != nil {
		t.Fatal(err)
	}
	command := exec.Command(node, "--import", filepath.Join(webRoot, "node_modules/tsx/dist/loader.mjs"), script)
	command.Env = append(os.Environ(), "TSX_DISABLE_CACHE=1", "TSX_TSCONFIG_PATH="+filepath.Join(webRoot, "tsconfig.app.json"), "AFTER53_G_HTTP="+httpServer.URL, "AFTER53_G_KB="+kb, "AFTER53_G_JWT="+jwt["member"])
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("Web consumer failed: %v %s", err, output)
	}
	j.Event("web-consumer", map[string]any{"success": true, "boundary": "actual API modules and image resolver/fetch; browser native decoding not exercised"})
	t.Log(string(output))
	// Concurrent HTTP list calls must return the exact persisted winner.
	var wg sync.WaitGroup
	errs := make(chan string, 12)
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, data, _, _ := request("/api/v1/system/tokens", "owner")
			if data["code"] != float64(0) {
				errs <- "list failed"
				return
			}
			for _, v := range data["data"].([]any) {
				row := v.(map[string]any)
				var beta string
				if err := reader.QueryRow("SELECT beta FROM t_ai_api_tokens WHERE tenant_id='owner' AND token=$1", row["token"]).Scan(&beta); err != nil || row["beta"] != beta || beta == "" {
					errs <- "winner mismatch"
				}
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatal(err)
	}
	var beta string
	if err := reader.QueryRow("SELECT beta FROM t_ai_api_tokens WHERE tenant_id='owner' AND token='preserved'").Scan(&beta); err != nil || beta != preserved {
		t.Fatal("existing beta rotated")
	}
	var other *string
	if err := reader.QueryRow("SELECT beta FROM t_ai_api_tokens WHERE tenant_id='foreign' AND token='missing'").Scan(&other); err != nil || other != nil {
		t.Fatal("foreign beta changed")
	}
	token, err := service.NewSystemService().CreateAPIToken("owner", &service.CreateAPITokenRequest{})
	if err != nil || token.Beta == nil || *token.Beta == utility.GenerateBetaAPIToken(token.Token) {
		t.Fatal("beta is not independent")
	}
	if err := db.Exec("ALTER TABLE t_ai_api_tokens ADD CONSTRAINT deny_beta CHECK (beta IS NULL OR beta='') NOT VALID").Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Exec("UPDATE t_ai_api_tokens SET beta=NULL WHERE token='missing' AND tenant_id='owner'").Error; err != nil {
		t.Fatal(err)
	}
	_, out, _, _ = request("/api/v1/system/tokens", "owner")
	if out["code"] == float64(0) {
		t.Fatal("SQL failure returned success")
	}
	var nilCount int
	if err := reader.QueryRow("SELECT count(*) FROM t_ai_api_tokens WHERE tenant_id='owner' AND token='missing' AND beta IS NULL").Scan(&nilCount); err != nil || nilCount != 1 {
		t.Fatal("failed SQL mutated beta")
	}
	// A separate MinIO client reads exact owned bytes after all denied requests.
	for _, k := range keys {
		object, err := client.GetObject(ctx, kb, k, minio.GetObjectOptions{})
		if err != nil {
			t.Fatal("independent object read")
		}
		data, err := io.ReadAll(object)
		object.Close()
		want := pngBytes.Bytes()
		if k == "invalid.svg" {
			want = []byte("<svg></svg>")
		}
		if err != nil || !bytes.Equal(data, want) {
			t.Fatal("owned object changed")
		}
	}
	j.Event("acceptance-readback", map[string]any{"jwtHTTP": true, "independentSQL": true, "independentObjects": true, "concurrentBetaRequests": 12, "objectReads": spy.reads.Load(), "indexBoundary": "controlled scoped rows; real SQL ownership; real MinIO bytes", "readDenialsZeroMutation": snapshot() == before})
}
