package service

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"strconv"
	"strings"
	"testing"

	"github.com/google/uuid"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/testsupport"
)

type documentFilesFixture struct {
	t       *testing.T
	db      *gorm.DB
	read    *sql.DB
	journal *testsupport.CaseJournal
	ids     map[string]string
	tokens  []string
	objects *documentFileObjects
	service *DocumentFileService
}

func newDocumentFilesFixture(t *testing.T) *documentFilesFixture {
	t.Helper()
	config := os.Getenv("MULTIRAG_AFTERAAB_F_CONFIG")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned F scratch PostgreSQL config required")
		}
		t.Skip("owned F scratch PostgreSQL config required")
	}
	journal := testsupport.NewCase(t)
	raw, err := os.ReadFile(config)
	if err != nil {
		t.Fatal("private F scratch config unavailable")
	}
	var cfg struct {
		Host, User, Password, Database string
		Port                           int
	}
	if json.Unmarshal(raw, &cfg) != nil || cfg.Host != "127.0.0.1" || cfg.Port <= 0 || !strings.HasPrefix(cfg.Database, "multirag_afteraab_f_") || cfg.User == "" {
		t.Fatal("explicit owned F TCP PostgreSQL database required")
	}
	endpoint := url.URL{Scheme: "postgres", User: url.UserPassword(cfg.User, cfg.Password), Host: net.JoinHostPort(cfg.Host, strconv.Itoa(cfg.Port)), Path: "/" + cfg.Database, RawQuery: "sslmode=disable&search_path=public"}
	tables := []string{"t_ai_tenants", "t_ai_users", "t_ai_user_tenants", "t_ai_knowledgebases", "t_ai_documents", "t_ai_files", "t_ai_file2documents", "t_ai_api4conversations", "t_ai_user_canvases", "t_ai_api_tokens"}
	journal.Event("reserve-owned-schema-before-SQL", map[string]any{"host": cfg.Host, "port": cfg.Port, "database": cfg.Database, "schema": "public", "tables": tables})
	db, err := gorm.Open(postgres.Open(endpoint.String()), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("owned F PostgreSQL connection failed")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = pool.Close() })
	read, err := sql.Open("pgx", endpoint.String())
	if err != nil {
		t.Fatal("independent owned F PostgreSQL connection failed")
	}
	t.Cleanup(func() { _ = read.Close() })
	old := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = old })
	models := []any{&entity.Tenant{}, &entity.User{}, &entity.UserTenant{}, &entity.Knowledgebase{}, &entity.Document{}, &entity.File{}, &entity.File2Document{}, &entity.API4Conversation{}, &entity.UserCanvas{}}
	if err := db.AutoMigrate(append(models, &entity.APIToken{})...); err != nil {
		t.Fatal("owned F schema migration failed")
	}
	for _, table := range tables {
		var count int
		if err := read.QueryRow("SELECT count(*) FROM " + table).Scan(&count); err != nil || count != 0 {
			t.Fatal("owned test database must contain no preexisting business rows", table, count)
		}
	}
	ids := map[string]string{}
	for _, name := range []string{"owner", "normal", "admin", "revoked", "invite", "outsider", "disabled-user", "foreign-owner", "inactive-owner", "token-tenant", "member-owner", "member-normal", "member-admin", "member-revoked", "member-invite", "member-disabled", "member-shared", "member-inactive-tenant", "kb", "foreign-kb", "inactive-kb", "disabled-tenant-kb", "doc", "inactive-doc", "foreign-doc", "inactive-kb-doc", "disabled-tenant-doc", "folder", "foreign-folder", "local-file", "shared-file", "link", "second-link", "canvas", "private-canvas", "session", "wrong-source-session", "missing-canvas-session", "private-canvas-session"} {
		ids[name] = strings.ReplaceAll(uuid.NewString(), "-", "")
	}
	journal.Event("reserve-owned-identities-before-insert", map[string]any{"sql_ids": ids, "controlled_object_addresses": []string{ids["kb"] + "/source.bin", ids["folder"] + "/local.bin", ids["foreign-folder"] + "/shared.bin"}, "actual_object_backend": false})
	f := &documentFilesFixture{t: t, db: db, read: read, journal: journal, ids: ids, objects: &documentFileObjects{data: []byte{0, 255, 10}}}
	f.tokens = []string{"afteraab-f-" + uuid.NewString(), "afteraab-f-" + uuid.NewString(), "afteraab-f-" + uuid.NewString(), "afteraab-f-" + uuid.NewString()}
	tokenHashes := []string{}
	for _, token := range f.tokens {
		tokenHashes = append(tokenHashes, fmt.Sprintf("%x", sha256.Sum256([]byte(token))))
	}
	journal.Event("reserve-owned-API-token-identities-before-insert", map[string]any{"token_sha256": tokenHashes, "tenants": []string{ids["token-tenant"], ids["owner"], ids["foreign-owner"], ids["inactive-owner"]}})
	f.service = &DocumentFileService{db: db, store: f.objects}
	allIDs := make([]string, 0, len(ids))
	for _, id := range ids {
		allIDs = append(allIDs, id)
	}
	t.Cleanup(func() {
		if err := db.Where("token IN ?", f.tokens).Delete(&entity.APIToken{}).Error; err != nil {
			t.Error("owned F API token cleanup failed")
		}
		for i := len(models) - 1; i >= 0; i-- {
			if err := db.Where("id IN ?", allIDs).Delete(models[i]).Error; err != nil {
				t.Error("owned F row cleanup failed")
			}
		}
		remaining := map[string]int{}
		for _, table := range tables {
			var count int
			column, values := "id", allIDs
			if table == "t_ai_api_tokens" {
				column, values = "token", f.tokens
			}
			if err := read.QueryRow("SELECT count(*) FROM "+table+" WHERE "+column+"=ANY($1)", values).Scan(&count); err != nil || count != 0 {
				t.Error("owned F row cleanup readback failed", table, count)
			}
			remaining[table] = count
		}
		journal.Event("cleanup-independent-SQL-readback", map[string]any{"remaining_owned_rows": remaining, "borrowed_TCP_service_preserved": true})
	})
	one := func(value string) *string { return &value }
	users := []entity.User{}
	for _, name := range []string{"owner", "normal", "admin", "revoked", "invite", "outsider", "disabled-user", "foreign-owner", "inactive-owner"} {
		users = append(users, entity.User{ID: ids[name], Nickname: name, Email: ids[name] + "@document-files.test", Status: one("1"), IsActive: true})
	}
	tenants := []entity.Tenant{{ID: ids["owner"], Status: one("1"), ParserIDs: "naive"}, {ID: ids["foreign-owner"], Status: one("1"), ParserIDs: "naive"}, {ID: ids["inactive-owner"], Status: one("0"), ParserIDs: "naive"}, {ID: ids["token-tenant"], Status: one("1"), ParserIDs: "naive"}}
	members := []entity.UserTenant{}
	for _, member := range []struct{ user, role, status string }{{"owner", "owner", "1"}, {"normal", "normal", "1"}, {"admin", "admin", "1"}, {"revoked", "normal", "0"}, {"invite", "invite", "1"}, {"disabled-user", "normal", "1"}} {
		members = append(members, entity.UserTenant{ID: ids["member-"+strings.TrimSuffix(member.user, "-user")], UserID: ids[member.user], TenantID: ids["owner"], Role: member.role, Status: one(member.status), InvitedBy: ids["owner"]})
	}
	members = append(members, entity.UserTenant{ID: ids["member-shared"], UserID: ids["normal"], TenantID: ids["foreign-owner"], Role: "normal", Status: one("1"), InvitedBy: ids["foreign-owner"]})
	members = append(members, entity.UserTenant{ID: ids["member-inactive-tenant"], UserID: ids["normal"], TenantID: ids["inactive-owner"], Role: "normal", Status: one("1"), InvitedBy: ids["inactive-owner"]})
	kbs := []entity.Knowledgebase{}
	for _, row := range []struct{ name, tenant, status string }{{"kb", "owner", "1"}, {"foreign-kb", "foreign-owner", "1"}, {"inactive-kb", "owner", "0"}, {"disabled-tenant-kb", "inactive-owner", "1"}} {
		kbs = append(kbs, entity.Knowledgebase{ID: ids[row.name], TenantID: ids[row.tenant], CreatedBy: ids[row.tenant], Name: row.name, Permission: "team", Status: one(row.status), ParserConfig: entity.JSONMap{}})
	}
	docs := []entity.Document{}
	for _, row := range []struct{ name, kb, status string }{{"doc", "kb", "1"}, {"inactive-doc", "kb", "0"}, {"foreign-doc", "foreign-kb", "1"}, {"inactive-kb-doc", "inactive-kb", "1"}, {"disabled-tenant-doc", "disabled-tenant-kb", "1"}} {
		docs = append(docs, entity.Document{ID: ids[row.name], KbID: ids[row.kb], CreatedBy: ids["owner"], ParserID: "naive", ParserConfig: entity.JSONMap{}, Type: "doc", SourceType: "local", Suffix: "bin", Name: one("source.bin"), Location: one("source.bin"), Status: one(row.status)})
	}
	files := []entity.File{
		{ID: ids["folder"], ParentID: ids["owner"], TenantID: ids["owner"], CreatedBy: ids["owner"], Name: "folder", Type: "folder"},
		{ID: ids["foreign-folder"], ParentID: ids["foreign-owner"], TenantID: ids["foreign-owner"], CreatedBy: ids["foreign-owner"], Name: "foreign-folder", Type: "folder"},
		{ID: ids["local-file"], ParentID: ids["folder"], TenantID: ids["owner"], CreatedBy: ids["owner"], Name: "local.bin", Type: "doc", SourceType: "local", Location: one("local.bin")},
		{ID: ids["shared-file"], ParentID: ids["foreign-folder"], TenantID: ids["foreign-owner"], CreatedBy: ids["foreign-owner"], Name: "shared.bin", Type: "doc", SourceType: "local", Location: one("shared.bin")},
	}
	canvases := []entity.UserCanvas{{ID: ids["canvas"], UserID: ids["owner"], Permission: "team", DSL: entity.JSONMap{}}, {ID: ids["private-canvas"], UserID: ids["foreign-owner"], Permission: "me", DSL: entity.JSONMap{}}}
	sessions := []entity.API4Conversation{
		{ID: ids["session"], DialogID: ids["canvas"], UserID: ids["normal"], Source: one("agent")},
		{ID: ids["wrong-source-session"], DialogID: ids["canvas"], UserID: ids["normal"], Source: one("chat")},
		{ID: ids["missing-canvas-session"], DialogID: ids["doc"], UserID: ids["normal"], Source: one("agent")},
		{ID: ids["private-canvas-session"], DialogID: ids["private-canvas"], UserID: ids["normal"], Source: one("agent")},
	}
	tokens := []entity.APIToken{{TenantID: ids["token-tenant"], Token: f.tokens[0]}, {TenantID: ids["owner"], Token: f.tokens[1]}, {TenantID: ids["foreign-owner"], Token: f.tokens[1]}, {TenantID: ids["inactive-owner"], Token: f.tokens[2]}, {TenantID: ids["owner"], Token: f.tokens[3]}, {TenantID: ids["inactive-owner"], Token: f.tokens[3]}}
	for _, rows := range []any{&users, &tenants, &members, &kbs, &docs, &files, &canvases, &sessions, &tokens} {
		if err := db.Create(rows).Error; err != nil {
			t.Fatal("owned F SQL seeding failed")
		}
	}
	if err := db.Model(&entity.User{}).Where("id=?", ids["disabled-user"]).Update("is_active", false).Error; err != nil {
		t.Fatal("owned F inactive actor setup failed")
	}
	return f
}

func (f *documentFilesFixture) snapshot() string {
	f.t.Helper()
	var state strings.Builder
	for _, table := range []string{"t_ai_tenants", "t_ai_users", "t_ai_user_tenants", "t_ai_knowledgebases", "t_ai_documents", "t_ai_files", "t_ai_file2documents", "t_ai_api4conversations", "t_ai_user_canvases", "t_ai_api_tokens"} {
		var raw string
		if err := f.read.QueryRow("SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text,'[]') FROM " + table + " AS t").Scan(&raw); err != nil {
			f.t.Fatal("independent F SQL snapshot failed")
		}
		state.WriteString(table + ":" + raw + "\n")
	}
	return state.String()
}

func TestDocumentFilesSQLAuthorizationAndBindings(t *testing.T) {
	f := newDocumentFilesFixture(t)
	ctx, ids := context.Background(), f.ids
	t.Run("API-token-original-tenant-and-zero-read-rejections", func(t *testing.T) {
		t.Setenv("DISABLE_SDK", "")
		callback := "afteraab-f-token-query-" + ids["token-tenant"]
		queries := 0
		f.journal.Event("reserve-owned-query-callback", map[string]any{"name": callback})
		if err := f.db.Callback().Query().Before("gorm:query").Register(callback, func(*gorm.DB) { queries++ }); err != nil {
			t.Fatal("query boundary registration failed")
		}
		t.Cleanup(func() {
			if err := f.db.Callback().Query().Remove(callback); err != nil {
				t.Error("query boundary cleanup failed")
			}
		})
		before := f.snapshot()
		for _, header := range []string{"Bearer " + f.tokens[0], "bearer " + f.tokens[0]} {
			got, err := ResolveAPITokenTenant(ctx, f.db, []string{header})
			if err != nil || got != ids["token-tenant"] {
				t.Fatal("API token tenant was replaced by owner identity", err)
			}
		}
		var matchingUsers int
		if err := f.read.QueryRow("SELECT count(*) FROM t_ai_users WHERE id=$1", ids["token-tenant"]).Scan(&matchingUsers); err != nil || matchingUsers != 0 {
			t.Fatal("tenant identity fixture unexpectedly equals a user identity")
		}
		for _, index := range []int{1, 2, 3} {
			got, err := ResolveAPITokenTenant(ctx, f.db, []string{"Bearer " + f.tokens[index]})
			if err == nil || got != "" {
				t.Fatal("ambiguous or disabled token tenant was accepted")
			}
		}
		for _, headers := range [][]string{nil, {}, {""}, {"Bearer"}, {"Bearer "}, {"Basic " + f.tokens[0]}, {"Bearer  " + f.tokens[0]}, {"Bearer\t" + f.tokens[0]}, {"Bearer " + f.tokens[0] + ",other"}, {"Bearer " + f.tokens[0] + "\n"}, {"Bearer " + f.tokens[0], "Bearer " + f.tokens[0]}} {
			calls := queries
			got, err := ResolveAPITokenTenant(ctx, f.db, headers)
			if err == nil || got != "" || queries != calls {
				t.Fatal("malformed API authorization reached SQL")
			}
		}
		t.Run("disabled-SDK", func(t *testing.T) {
			t.Setenv("DISABLE_SDK", "1")
			calls := queries
			if got, err := ResolveAPITokenTenant(ctx, f.db, []string{"Bearer " + f.tokens[0]}); err == nil || got != "" || queries != calls {
				t.Fatal("disabled SDK reached token SQL")
			}
		})
		if f.snapshot() != before {
			t.Fatal("API token authorization changed SQL rows")
		}
		f.journal.Event("API-token-scope-independent-readback", map[string]any{"original_tenant_preserved": true, "tenant_has_user_row": false, "duplicate_active_denied": true, "duplicate_active_inactive_denied": true, "malformed_header_SQL_queries": 0, "SQL_unchanged": true})
	})
	for _, test := range []struct {
		name, user, doc string
		download        bool
		want            error
	}{
		{"normal-preview", "normal", "doc", false, nil}, {"owner-download", "owner", "doc", true, nil}, {"admin-download", "admin", "doc", true, nil},
		{"normal-download-denied", "normal", "doc", true, ErrDocumentDownloadDenied}, {"revoked", "revoked", "doc", false, ErrDocumentFileUnavailable},
		{"invited", "invite", "doc", false, ErrDocumentFileUnavailable}, {"outsider", "outsider", "doc", false, ErrDocumentFileUnavailable},
		{"disabled-actor", "disabled-user", "doc", false, ErrDocumentFileUnavailable}, {"inactive-document", "normal", "inactive-doc", false, ErrDocumentFileUnavailable},
		{"inactive-dataset", "normal", "inactive-kb-doc", false, ErrDocumentFileUnavailable}, {"inactive-tenant", "normal", "disabled-tenant-doc", false, ErrDocumentFileUnavailable},
		{"foreign-document", "owner", "foreign-doc", false, ErrDocumentFileUnavailable},
	} {
		t.Run(test.name, func(t *testing.T) {
			before, calls := f.snapshot(), len(f.objects.reads)
			got, err := f.service.GetDocumentPreview(ctx, ids[test.user], ids[test.doc], test.download)
			if !errors.Is(err, test.want) {
				t.Fatalf("preview error %v, want %v", err, test.want)
			}
			if test.want != nil {
				if got != nil || len(f.objects.reads) != calls {
					t.Fatal("denied preview reached object storage")
				}
			} else if got == nil || !bytes.Equal(got.Data, f.objects.data) || got.FileName != "source.bin" || got.ContentType != "application/octet-stream" || len(f.objects.reads) != calls+1 || f.objects.reads[calls] != (documentFileRead{ids["kb"], "source.bin"}) {
				t.Fatal("authorized preview lost source bytes or exact address")
			}
			if f.snapshot() != before {
				t.Fatal("read changed SQL rows")
			}
		})
	}
	for _, test := range []struct {
		name, tenant, kb, doc string
		want                  error
	}{
		{"tenant-download", "owner", "kb", "doc", nil}, {"mismatched-dataset", "owner", "foreign-kb", "doc", ErrDocumentFileUnavailable},
		{"foreign-document", "owner", "foreign-kb", "foreign-doc", ErrDocumentFileUnavailable}, {"foreign-tenant", "foreign-owner", "kb", "doc", ErrDocumentFileUnavailable},
		{"inactive-document", "owner", "kb", "inactive-doc", ErrDocumentFileUnavailable}, {"inactive-dataset", "owner", "inactive-kb", "inactive-kb-doc", ErrDocumentFileUnavailable},
		{"inactive-tenant", "inactive-owner", "disabled-tenant-kb", "disabled-tenant-doc", ErrDocumentFileUnavailable},
	} {
		t.Run(test.name, func(t *testing.T) {
			before, calls := f.snapshot(), len(f.objects.reads)
			got, err := f.service.DownloadDocument(ctx, ids[test.tenant], ids[test.kb], ids[test.doc])
			if !errors.Is(err, test.want) || test.want != nil && (got != nil || len(f.objects.reads) != calls) || test.want == nil && (got == nil || len(f.objects.reads) != calls+1) {
				t.Fatal("tenant download escaped exact active scope", err)
			}
			if f.snapshot() != before {
				t.Fatal("download changed SQL rows")
			}
		})
	}
	t.Run("linked-sources", func(t *testing.T) {
		one := func(value string) *string { return &value }
		for _, test := range []struct{ name, file, bucket, key string }{{"local", "local-file", "folder", "local.bin"}, {"registered-shared-tenant-source", "shared-file", "foreign-folder", "shared.bin"}} {
			t.Run(test.name, func(t *testing.T) {
				link := entity.File2Document{ID: ids["link"], FileID: one(ids[test.file]), DocumentID: one(ids["doc"])}
				if err := f.db.Create(&link).Error; err != nil {
					t.Fatal(err)
				}
				t.Cleanup(func() {
					if err := f.db.Where("id=?", ids["link"]).Delete(&entity.File2Document{}).Error; err != nil {
						t.Error("owned link cleanup failed")
					}
				})
				before, calls := f.snapshot(), len(f.objects.reads)
				got, err := f.service.GetDocumentPreview(ctx, ids["normal"], ids["doc"], false)
				if err != nil || got == nil || len(f.objects.reads) != calls+1 || f.objects.reads[calls] != (documentFileRead{ids[test.bucket], test.key}) {
					t.Fatal("registered file binding did not resolve exact source", err)
				}
				if f.snapshot() != before {
					t.Fatal("source read changed SQL rows")
				}
			})
		}
		link := entity.File2Document{ID: ids["link"], FileID: one(ids["local-file"]), DocumentID: one(ids["doc"])}
		if err := f.db.Create(&link).Error; err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			if err := f.db.Where("id IN ?", []string{ids["link"], ids["second-link"]}).Delete(&entity.File2Document{}).Error; err != nil {
				t.Error("owned links cleanup failed")
			}
		})
		for _, test := range []struct {
			name    string
			prepare func() error
			restore func() error
		}{
			{"forged-parent-foreign-tenant", func() error {
				return f.db.Model(&entity.File{}).Where("id=?", ids["local-file"]).Update("parent_id", ids["foreign-folder"]).Error
			}, func() error {
				return f.db.Model(&entity.File{}).Where("id=?", ids["local-file"]).Update("parent_id", ids["folder"]).Error
			}},
			{"ambiguous-links", func() error {
				return f.db.Create(&entity.File2Document{ID: ids["second-link"], FileID: one(ids["shared-file"]), DocumentID: one(ids["doc"])}).Error
			}, func() error { return f.db.Where("id=?", ids["second-link"]).Delete(&entity.File2Document{}).Error }},
			{"nil-file-id", func() error {
				return f.db.Model(&entity.File2Document{}).Where("id=?", ids["link"]).Update("file_id", nil).Error
			}, func() error {
				return f.db.Model(&entity.File2Document{}).Where("id=?", ids["link"]).Update("file_id", ids["local-file"]).Error
			}},
			{"invalid-local-path", func() error {
				return f.db.Model(&entity.File{}).Where("id=?", ids["local-file"]).Update("location", "../secret").Error
			}, func() error {
				return f.db.Model(&entity.File{}).Where("id=?", ids["local-file"]).Update("location", "local.bin").Error
			}},
		} {
			t.Run(test.name, func(t *testing.T) {
				if err := test.prepare(); err != nil {
					t.Fatal("owned source setup failed")
				}
				t.Cleanup(func() {
					if err := test.restore(); err != nil {
						t.Error("owned source restoration failed")
					}
				})
				before, calls := f.snapshot(), len(f.objects.reads)
				got, err := f.service.GetDocumentPreview(ctx, ids["normal"], ids["doc"], false)
				if !errors.Is(err, ErrDocumentFileUnavailable) || got != nil || len(f.objects.reads) != calls {
					t.Fatal("invalid source binding reached object storage", err)
				}
				if f.snapshot() != before {
					t.Fatal("denial changed SQL rows")
				}
			})
		}
		t.Run("nonlocal-link-keeps-document-source", func(t *testing.T) {
			if err := f.db.Model(&entity.File{}).Where("id=?", ids["local-file"]).Update("source_type", "knowledgebase").Error; err != nil {
				t.Fatal("nonlocal source setup failed")
			}
			t.Cleanup(func() {
				if err := f.db.Model(&entity.File{}).Where("id=?", ids["local-file"]).Update("source_type", "local").Error; err != nil {
					t.Error("nonlocal source restoration failed")
				}
			})
			before, calls := f.snapshot(), len(f.objects.reads)
			got, err := f.service.GetDocumentPreview(ctx, ids["normal"], ids["doc"], false)
			if err != nil || got == nil || len(f.objects.reads) != calls+1 || f.objects.reads[calls] != (documentFileRead{ids["kb"], "source.bin"}) {
				t.Fatal("nonlocal file link changed document source", err)
			}
			if f.snapshot() != before {
				t.Fatal("nonlocal source read changed SQL rows")
			}
		})
	})
	t.Run("artifact-session-authorization", func(t *testing.T) {
		filename, run := ids["local-file"]+".csv", ids["doc"]
		f.journal.Event("reserve-controlled-artifact-identities", map[string]any{"filename": filename, "run_id": run, "session_ids": []string{ids["session"], ids["wrong-source-session"], ids["missing-canvas-session"], ids["private-canvas-session"], ids["doc"]}, "ownership_reader": "controlled", "object_store": "controlled"})
		for _, test := range []struct {
			name, session string
			revoke        bool
			want          error
		}{
			{"active-team-session", "session", false, nil}, {"wrong-conversation-source", "wrong-source-session", false, ErrDocumentFileUnavailable},
			{"missing-canvas", "missing-canvas-session", false, ErrDocumentFileUnavailable}, {"private-canvas", "private-canvas-session", false, ErrDocumentFileUnavailable},
			{"missing-conversation", "doc", false, ErrDocumentFileUnavailable}, {"revoked-member", "session", true, ErrDocumentFileUnavailable},
		} {
			t.Run(test.name, func(t *testing.T) {
				session := ids[test.session]
				raw, err := json.Marshal(artifactBinding{OwnerID: ids["normal"], RunID: run, SessionID: &session})
				if err != nil {
					t.Fatal(err)
				}
				f.service.bindingReader = func(context.Context, string) (string, error) { return string(raw), nil }
				if test.revoke {
					if err := f.db.Model(&entity.UserTenant{}).Where("id=?", ids["member-normal"]).Update("status", "0").Error; err != nil {
						t.Fatal(err)
					}
					t.Cleanup(func() {
						if err := f.db.Model(&entity.UserTenant{}).Where("id=?", ids["member-normal"]).Update("status", "1").Error; err != nil {
							t.Error("owned membership restoration failed")
						}
					})
				}
				before, calls := f.snapshot(), len(f.objects.reads)
				got, err := f.service.GetDocumentArtifact(ctx, ids["normal"], filename, run, &session)
				if !errors.Is(err, test.want) || test.want != nil && (got != nil || len(f.objects.reads) != calls) || test.want == nil && (got == nil || len(f.objects.reads) != calls+1 || got.ContentType != "text/csv") {
					t.Fatal("artifact escaped current session access", err)
				}
				if f.snapshot() != before {
					t.Fatal("artifact read changed SQL rows")
				}
			})
		}
	})
	f.journal.Event("authorization-and-source-readback", map[string]any{"SQL_reads": "independent connection snapshots unchanged", "object_reads": f.objects.reads, "controlled_store": true, "real_SQL_authorization": true})
}
