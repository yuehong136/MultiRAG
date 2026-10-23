package router

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
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
	"github.com/google/uuid"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/cli"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
)

func TestDocumentListHTTPCLIAndSQL(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_AFTER9D_B_DOCS_DSN")
	if dsn == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned document-list scratch database required")
		}
		t.Skip("requires owned document-list scratch database")
	}
	address, err := url.Parse(dsn)
	if err != nil || !strings.HasPrefix(strings.TrimPrefix(address.Path, "/"), "multirag_after9d_b_docs_") {
		t.Fatal("unowned database")
	}
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	cfg := filepath.Join(t.TempDir(), "docs.yaml")
	if err := os.WriteFile(cfg, []byte("server:\n  secret_key: owned-document-list-secret-key-32-characters\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(cfg); err != nil {
		t.Fatal(err)
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal(err)
	}
	pool, _ := db.DB()
	t.Cleanup(func() { pool.Close() })
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { readback.Close() })
	prev := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = prev })
	previousAdmin := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned document list test")
	t.Cleanup(func() { local.SetAdminStatus(previousAdmin.Status, previousAdmin.Reason) })
	if err := db.AutoMigrate(&entity.User{}, &entity.UserTenant{}, &entity.APIToken{}, &entity.Knowledgebase{}, &entity.Document{}, &entity.File2Document{}, &entity.UserCanvas{}); err != nil {
		t.Fatal(err)
	}
	active, inactive, access := "1", "0", "owned-document-session"
	for _, id := range []string{"doc-owner", "doc-member", "doc-foreign", "doc-revoked"} {
		if err := db.Create(&entity.User{ID: id, Email: id + "@example.invalid", Nickname: id, AccessToken: &access, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := db.Create(&entity.APIToken{TenantID: id, Token: "document-token-" + id}).Error; err != nil {
			t.Fatal(err)
		}
		tenant, status := "doc-owner", active
		if id == "doc-foreign" {
			tenant = "doc-foreign"
		}
		if id == "doc-revoked" {
			status = inactive
		}
		if err := db.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: tenant, Role: "normal", InvitedBy: "doc-owner", Status: &status}).Error; err != nil {
			t.Fatal(err)
		}
	}
	ids := map[string]string{}
	for _, kind := range []string{"shared", "private", "foreign", "disabled"} {
		id, err := uuid.NewUUID()
		if err != nil {
			t.Fatal(err)
		}
		ids[kind] = strings.ReplaceAll(id.String(), "-", "")
		tenant, permission, status := "doc-owner", "team", active
		if kind == "private" {
			permission = "me"
		}
		if kind == "foreign" {
			tenant = "doc-foreign"
		}
		if kind == "disabled" {
			status = inactive
		}
		if err := db.Create(&entity.Knowledgebase{ID: ids[kind], Name: kind, TenantID: tenant, Permission: permission, CreatedBy: tenant, EmbdID: "none", ParserConfig: entity.JSONMap{}, Status: &status}).Error; err != nil {
			t.Fatal(err)
		}
	}
	title, foreignTitle := "owned pipeline", "foreign private pipeline"
	for _, canvas := range []entity.UserCanvas{{ID: "pipeline", UserID: "doc-owner", Title: &title, DSL: entity.JSONMap{}}, {ID: "foreign-pipeline", UserID: "doc-foreign", Title: &foreignTitle, DSL: entity.JSONMap{}}} {
		if err := db.Create(&canvas).Error; err != nil {
			t.Fatal(err)
		}
	}
	stamp := int64(1700000000123)
	date := time.Date(2026, 10, 24, 3, 0, 0, 0, time.UTC)
	expectedDate := date.In(time.Local).Format("2006-01-02 15:04:05")
	for i := 0; i < 6; i++ {
		name, run := fmt.Sprintf("report-%d", i), strconv.Itoa(i%5)
		pipeline := "pipeline"
		if i == 1 {
			pipeline = "missing"
		}
		if i == 2 {
			pipeline = "foreign-pipeline"
		}
		status := active
		if i == 5 {
			status = inactive
		}
		var metadata *entity.JSONMap
		if i == 0 {
			fields := entity.JSONMap{"kind": "rich", "enabled": false}
			metadata = &fields
		}
		doc := entity.Document{ID: fmt.Sprintf("document-%d", i), KbID: ids["shared"], ParserID: "naive", PipelineID: &pipeline, ParserConfig: entity.JSONMap{"enabled": false, "delimiter": "\\n"}, Type: "txt", CreatedBy: "doc-owner", Name: &name, Size: 10, TokenNum: 7, ChunkNum: 3, Progress: 0.5, ProcessBeginAt: &date, ProcessDuration: 1.5, MetaFields: metadata, Suffix: "txt", Run: &run, Status: &status, BaseModel: entity.BaseModel{CreateTime: &stamp, UpdateTime: &stamp, CreateDate: &date, UpdateDate: &date}}
		if err := db.Create(&doc).Error; err != nil {
			t.Fatal(err)
		}
	}
	linked := "document-1"
	for i := 0; i < 2; i++ {
		fileID := fmt.Sprintf("file-%d", i)
		if err := db.Create(&entity.File2Document{ID: fileID, FileID: &fileID, DocumentID: &linked}).Error; err != nil {
			t.Fatal(err)
		}
	}
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	(&Router{authHandler: handler.NewAuthHandler(), documentHandler: handler.NewDocumentHandler(service.NewDocumentService())}).Setup(app)
	httpServer := httptest.NewServer(app)
	defer httpServer.Close()
	request := func(id, query, actor string) map[string]any {
		t.Helper()
		w := httptest.NewRecorder()
		req := httptest.NewRequest("GET", "/api/v1/datasets/"+id+"/documents?"+query, nil)
		if actor != "" {
			req.Header.Set("Authorization", "Bearer document-token-"+actor)
		}
		app.ServeHTTP(w, req)
		var result map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil {
			t.Fatalf("invalid JSON %s", w.Body)
		}
		return result
	}
	snapshot := func() string {
		t.Helper()
		var raw string
		if err := readback.QueryRow(`SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM t_ai_documents x`).Scan(&raw); err != nil {
			t.Fatal(err)
		}
		return raw
	}
	before := snapshot()
	seen := map[string]bool{}
	for page := 1; page <= 3; page++ {
		out := request(ids["shared"], fmt.Sprintf("page=%d&page_size=2", page), "doc-member")
		if out["code"] != float64(0) {
			t.Fatal(out)
		}
		data := out["data"].(map[string]any)
		if data["total"] != float64(5) {
			t.Fatal("join changed total", data)
		}
		for _, value := range data["docs"].([]any) {
			row := value.(map[string]any)
			id := row["id"].(string)
			if seen[id] {
				t.Fatal("duplicated document", id)
			}
			seen[id] = true
			if row["token_count"] != float64(7) || row["chunk_count"] != float64(3) || row["create_time"] != float64(stamp) || row["create_date"] != expectedDate || row["process_begin_at"] != expectedDate || row["nickname"] != "doc-owner" || row["parser_config"].(map[string]any)["enabled"] != false {
				t.Fatal("enriched contract missing", row)
			}
			wantRun := []string{"UNSTART", "RUNNING", "CANCEL", "DONE", "FAIL"}
			number, _ := strconv.Atoi(strings.TrimPrefix(id, "document-"))
			if row["run"] != wantRun[number] {
				t.Fatal("run status mismatch", row)
			}
			if id == "document-0" && (row["pipeline_name"] != "owned pipeline" || row["meta_fields"].(map[string]any)["kind"] != "rich") {
				t.Fatal("joined fields missing", row)
			}
			if id == "document-2" && row["pipeline_name"] != "" {
				t.Fatal("foreign pipeline title leaked", row)
			}
		}
	}
	if len(seen) != 5 || !seen["document-0"] || !seen["document-1"] {
		t.Fatal("file join dropped orphan or duplicated linked document")
	}
	out := request(ids["shared"], "page=99&page_size=2", "doc-owner")
	data := out["data"].(map[string]any)
	if data["total"] != float64(5) || len(data["docs"].([]any)) != 0 {
		t.Fatal("page total mismatch")
	}
	out = request(ids["shared"], "keywords=missing", "doc-owner")
	if out["data"].(map[string]any)["total"] != float64(0) {
		t.Fatal("keyword filter mismatch")
	}
	out = request(ids["shared"], "return_empty_metadata=false", "doc-owner")
	for _, value := range out["data"].(map[string]any)["docs"].([]any) {
		row := value.(map[string]any)
		if row["id"] != "document-0" {
			if _, present := row["meta_fields"]; present {
				t.Fatal("empty metadata flag ignored")
			}
		}
	}
	for _, tc := range []struct{ kind, actor string }{{"private", "doc-member"}, {"foreign", "doc-owner"}, {"disabled", "doc-owner"}, {"shared", "doc-revoked"}, {"shared", "doc-foreign"}, {"shared", ""}} {
		out := request(ids[tc.kind], "", tc.actor)
		if out["code"] == float64(0) {
			t.Fatal("unauthorized dataset visible", tc)
		}
		if snapshot() != before {
			t.Fatal("denial changed documents")
		}
	}
	endpoint, _ := url.Parse(httpServer.URL)
	port, _ := strconv.Atoi(endpoint.Port())
	client := cli.NewMultiRAGClient("user")
	client.HTTPClient.Host = endpoint.Hostname()
	client.HTTPClient.Port = port
	client.HTTPClient.APIKey = "document-token-doc-owner"
	command, err := cli.NewParser("LIST DOCUMENTS FROM '" + ids["shared"] + "';").Parse(false)
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.ExecuteCommand(command)
	if err != nil {
		t.Fatal(err)
	}
	if len(result.(*cli.ListDocumentsResponse).Docs) != 5 || result.(*cli.ListDocumentsResponse).Docs[0]["token_count"] != float64(7) {
		t.Fatal("actual CLI consumer lost enriched contract")
	}
	binary := os.Getenv("MULTIRAG_AFTER9D_B_CLI_BINARY")
	if binary == "" {
		t.Fatal("actual CLI binary required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	output, err := exec.CommandContext(ctx, binary, "-h", endpoint.Host, "-t", "document-token-doc-owner", "-o", "json", "LIST DOCUMENTS FROM '"+ids["shared"]+"';").CombinedOutput()
	if err != nil || !strings.Contains(string(output), `"token_count": 7`) {
		t.Fatalf("CLI binary failed %v %s", err, output)
	}
	if snapshot() != before {
		t.Fatal("read route/CLI changed document state")
	}
	for _, field := range []string{"meta_fields", "parser_config"} {
		if err := db.Exec("UPDATE t_ai_documents SET " + field + "='[]'::jsonb WHERE id='document-0'").Error; err != nil {
			t.Fatal(err)
		}
		malformed := snapshot()
		out := request(ids["shared"], "", "doc-owner")
		if out["code"] == float64(0) || snapshot() != malformed {
			t.Fatal("malformed JSON silently accepted or mutated", field)
		}
		if err := db.Exec("UPDATE t_ai_documents SET " + field + "='{}'::jsonb WHERE id='document-0'").Error; err != nil {
			t.Fatal(err)
		}
	}
}
