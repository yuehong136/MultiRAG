package handler

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/storage"
	"multirag/internal/testsupport"
	"multirag/internal/testsupport/scratch"
	"multirag/internal/utility"
)

type canonicalHTTPDocumentStore struct {
	storage.Storage
	reads atomic.Int64
}

func (s *canonicalHTTPDocumentStore) Get(bucket, key string, tenant ...string) ([]byte, error) {
	s.reads.Add(1)
	return s.Storage.Get(bucket, key, tenant...)
}

func TestCanonicalDocumentFilesHTTPMySQLMinio(t *testing.T) {
	f := scratch.New(t)
	previousDB, previousStore := dao.DB, storage.GetStorageFactory().GetStorage()
	dao.DB = f.DB
	objects := &canonicalHTTPDocumentStore{Storage: f.Store}
	storage.GetStorageFactory().SetStorage(objects)
	t.Cleanup(func() { dao.DB = previousDB; storage.GetStorageFactory().SetStorage(previousStore) })
	if err := f.DB.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}, &entity.Knowledgebase{}, &entity.Document{}, &entity.File{}, &entity.File2Document{}, &entity.APIToken{}); err != nil {
		t.Fatal(err)
	}
	secret := f.Prefix + "-document-auth"
	cfg := server.GetConfig()
	if cfg == nil {
		path := filepath.Join(t.TempDir(), "document-service.json")
		f.Journal.Event("reserve-document-config", map[string]any{"path": path})
		raw, _ := json.Marshal(map[string]any{"server": map[string]any{"secret_key": secret}})
		if err := os.WriteFile(path, raw, 0600); err != nil {
			t.Fatal(err)
		}
		if err := server.FromConfigFile(path); err != nil {
			t.Fatal(err)
		}
		cfg = server.GetConfig()
	}
	oldSecret := cfg.Server.SecretKey
	cfg.Server.SecretKey = &secret
	t.Cleanup(func() { cfg.Server.SecretKey = oldSecret })
	license := local.GetAdminStatus()
	local.SetAdminStatus(0, "owned document fixture")
	t.Cleanup(func() { local.SetAdminStatus(license.Status, license.Reason) })
	ids := map[string]string{}
	tokens := map[string]string{}
	for _, name := range []string{"owner", "member", "invite", "revoked", "foreign", "disabled"} {
		id := f.Prefix + name
		ids[name] = id
		active := "1"
		if name == "disabled" {
			active = "0"
		}
		access := utility.GenerateToken()
		if err := f.DB.Create(&entity.User{ID: id, Nickname: name, Email: id + "@fixture.invalid", IsActive: active, Status: &active, AccessToken: &access}).Error; err != nil {
			t.Fatal(err)
		}
		token, err := utility.DumpAccessToken(access, secret)
		if err != nil {
			t.Fatal(err)
		}
		tokens[name] = token
	}
	active := "1"
	if err := f.DB.Create(&entity.Tenant{ID: ids["owner"], Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	for _, member := range []struct{ name, role, status string }{{"member", "normal", "1"}, {"invite", "invite", "1"}, {"revoked", "normal", "0"}} {
		status := member.status
		if err := f.DB.Create(&entity.UserTenant{ID: utility.GenerateToken(), UserID: ids[member.name], TenantID: ids["owner"], Role: member.role, Status: &status}).Error; err != nil {
			t.Fatal(err)
		}
	}
	kb, doc := f.Prefix+"kb", f.Prefix+"doc"
	filename := "source.json"
	data := []byte(`{"code":109,"message":"source, not an envelope","data":null}`)
	if err := f.DB.Create(&entity.Knowledgebase{ID: kb, TenantID: ids["owner"], CreatedBy: ids["owner"], Name: kb, ParserID: "naive", ParserConfig: entity.JSONMap{}, Permission: "team", Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	if err := f.DB.Create(&entity.Document{ID: doc, KbID: kb, Name: &filename, Location: &filename, CreatedBy: ids["owner"], ParserID: "naive", ParserConfig: entity.JSONMap{}, Status: &active}).Error; err != nil {
		t.Fatal(err)
	}
	if err := f.Store.Put(kb, filename, data); err != nil {
		t.Fatal(err)
	}
	h := NewDocumentHandler(service.NewDocumentService(), service.NewDatasetService())
	gin.SetMode(gin.TestMode)
	app := gin.New()
	app.Use(NewAuthHandler().AuthMiddleware())
	app.GET("/api/v1/documents/:id/preview", h.GetDocumentPreview)
	app.GET("/api/v1/datasets/:dataset_id/documents/:document_id", h.DownloadDocument)
	app.GET("/api/v1/documents/artifact/:filename", h.GetDocumentArtifact)
	httpServer := testsupport.NewHTTP(t, app)
	request := func(actor, path string) (int, []byte, http.Header) {
		t.Helper()
		req, err := http.NewRequestWithContext(t.Context(), "GET", httpServer.URL+path, nil)
		if err != nil {
			t.Fatal(err)
		}
		if actor != "" {
			req.Header.Set("Authorization", tokens[actor])
		}
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		raw, err := io.ReadAll(res.Body)
		if err != nil {
			t.Fatal(err)
		}
		return res.StatusCode, raw, res.Header
	}
	for _, path := range []string{"/api/v1/documents/" + doc + "/preview", "/api/v1/datasets/" + kb + "/documents/" + doc} {
		wantContentType := "application/octet-stream"
		if strings.HasSuffix(path, "/preview") {
			wantContentType = "application/json"
		}
		for _, actor := range []string{"", "owner", "member", "invite", "revoked", "foreign", "disabled"} {
			before := objects.reads.Load()
			status, raw, headers := request(actor, path)
			if actor == "owner" || actor == "member" {
				if status != 200 || !bytes.Equal(raw, data) || objects.reads.Load() != before+1 || headers.Get("Content-Type") != wantContentType {
					t.Fatal("authorized source bytes changed", actor, status, string(raw), headers)
				}
			} else {
				if bytes.Equal(raw, data) || objects.reads.Load() != before {
					t.Fatal("unauthorized source read", actor, status, string(raw))
				}
			}
		}
	}
	before := objects.reads.Load()
	_, raw, _ := request("owner", "/api/v1/datasets/foreign/documents/"+doc)
	if bytes.Equal(raw, data) || objects.reads.Load() != before {
		t.Fatal("substituted dataset read object")
	}
	empty := []byte{}
	if err := f.Store.Put(kb, filename, empty); err != nil {
		t.Fatal(err)
	}
	_, raw, _ = request("owner", "/api/v1/datasets/"+kb+"/documents/"+doc)
	if len(raw) != 0 {
		t.Fatal("empty raw object was projected to an error", string(raw))
	}
	f.ReadObject(t, kb, filename, empty)
	// The frozen artifact endpoint has an authenticated global basename namespace.
	// Its removed owner/run/session binding protocol is not represented as isolation.
	artifactBucket := f.Prefix + "artifacts"
	t.Setenv("SANDBOX_ARTIFACT_BUCKET", artifactBucket)
	if err := f.Store.Put(artifactBucket, "safe.svg", []byte("<svg>artifact</svg>")); err != nil {
		t.Fatal(err)
	}
	_, raw, headers := request("foreign", "/api/v1/documents/artifact/safe.svg")
	if string(raw) != "<svg>artifact</svg>" || headers.Get("Content-Disposition") != "attachment" || headers.Get("X-Content-Type-Options") != "nosniff" {
		t.Fatal("canonical global artifact/attachment policy changed", string(raw), headers)
	}
	f.Journal.Event("canonical-document-http-independent-readback", map[string]any{"raw_source_preserved": true, "foreign_invited_revoked_disabled_denied_before_storage": true, "artifact_authenticated_global_namespace": true, "artifact_tenant_ownership": false})
}

func TestCanonicalDocumentCRUDScopeHTTPMySQL(t *testing.T) {
	f := newCanonicalHTTPFixture(t)
	doc := f.document(t, "shared", "protected.txt")
	h := NewDocumentHandler(service.NewDocumentService(), service.NewDatasetService())
	app := f.app()
	app.POST("/documents", h.CreateDocument)
	app.GET("/documents/thumbnails", h.GetThumbnail)
	app.GET("/documents/:id", h.GetDocumentByID)
	app.PUT("/documents/:id", h.UpdateDocument)
	app.DELETE("/documents/:id", h.DeleteDocument)
	url := f.serve(t, app)
	snapshot := func() (string, int) {
		t.Helper()
		var name string
		var count int
		if err := f.Read.QueryRow("SELECT name FROM document WHERE id=?", doc).Scan(&name); err != nil {
			t.Fatal(err)
		}
		if err := f.Read.QueryRow("SELECT count(*) FROM document").Scan(&count); err != nil {
			t.Fatal(err)
		}
		return name, count
	}
	for _, actor := range []string{"", "invite", "revoked", "foreign", "disabled"} {
		beforeName, beforeCount := snapshot()
		for _, path := range []string{"/documents/" + doc, "/documents/thumbnails?doc_ids=" + doc} {
			_, result, raw, _ := f.request(t, url, actor, "GET", path, nil)
			if bytes.Contains(raw, []byte("protected.txt")) || result["data"] != nil {
				t.Fatal("unauthorized document detail", actor, string(raw))
			}
		}
		for _, method := range []string{"PUT", "DELETE"} {
			_, result, _, _ := f.request(t, url, actor, method, "/documents/"+doc, map[string]any{"name": "forbidden"})
			if result["message"] == "updated successfully" || result["message"] == "deleted successfully" {
				t.Fatal("unauthorized mutation", actor, method, result)
			}
		}
		_, result, _, _ := f.request(t, url, actor, "POST", "/documents", map[string]any{"name": "forbidden", "kb_id": f.datasets["shared"], "parser_id": "naive", "created_by": f.ids["owner"]})
		if result["message"] == "created successfully" {
			t.Fatal("unauthorized create", actor, result)
		}
		afterName, afterCount := snapshot()
		if beforeName != afterName || beforeCount != afterCount {
			t.Fatal("denied document CRUD changed SQL", actor, afterName, afterCount)
		}
	}
	for _, actor := range []string{"owner", "member"} {
		status, result, _, _ := f.request(t, url, actor, "GET", "/documents/"+doc, nil)
		if status != 200 || result["data"].(map[string]any)["id"] != doc {
			t.Fatal("authorized detail", actor, result)
		}
	}
	_, changed, _, _ := f.request(t, url, "member", "PUT", "/documents/"+doc, map[string]any{"name": "member update"})
	if changed["message"] != "updated successfully" {
		t.Fatal("authorized update", changed)
	}
	name, _ := snapshot()
	if name != "member update" {
		t.Fatal("update readback", name)
	}
	for _, name := range []string{"created one", "created two"} {
		status, result, _, _ := f.request(t, url, "member", "POST", "/documents", map[string]any{"name": name, "kb_id": f.datasets["shared"], "parser_id": "naive", "created_by": f.ids["foreign"]})
		if status != 200 || result["message"] != "created successfully" {
			t.Fatal("original document create", status, result)
		}
		created := result["data"].(map[string]any)
		id, _ := created["id"].(string)
		if id == "" {
			t.Fatal("document created without ID", created)
		}
		var actor string
		if err := f.Read.QueryRow("SELECT created_by FROM document WHERE id=?", id).Scan(&actor); err != nil || actor != f.ids["member"] {
			t.Fatal("create trusted forged created_by", actor, err)
		}
	}
	f.Journal.Event("canonical-document-crud-scope-readback", map[string]any{"foreign_invite_revoked_disabled_all_single_record_operations_denied": true, "independent_sql_zero_mutation_readback": true, "create_binds_authenticated_actor": true})
}
