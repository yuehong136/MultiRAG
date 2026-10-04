package skills

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/service"
)

func checkLiveFences(t *testing.T, s *Service) {
	t.Helper()
	ctx := context.Background()
	tenant := newID()
	goOp := makeOperation(tenant, "reindex", newID(), JSON{})
	foreign := makeOperation(tenant, "reindex", newID(), JSON{})
	foreign.BackendOwner = "python"
	staging := makeOperation(tenant, "install", newID(), JSON{})
	staging.Phase = "staging"
	for _, op := range []Operation{goOp, foreign, staging} {
		if e := s.DB.Create(&op).Error; e != nil {
			t.Fatal(e)
		}
	}
	defer s.DB.Where("tenant_id = ?", tenant).Delete(&Operation{})
	first, e := s.Claim(ctx, "first")
	if e != nil || first == nil || first.ID != goOp.ID {
		t.Fatal("owner/staging claim failed", e)
	}
	if another, e := s.Claim(ctx, "second"); e != nil || another != nil {
		t.Fatal("unexpired claim stolen", e)
	}
	if e = s.DB.Model(&Operation{}).Where("id = ?", first.ID).Update("lease_expires_at", gorm.Expr("clock_timestamp() - INTERVAL '1 second'")).Error; e != nil {
		t.Fatal(e)
	}
	second, e := s.Claim(ctx, "second")
	if e != nil || second == nil || second.Revision <= first.Revision {
		t.Fatal("expired lease not reclaimed", e)
	}
	called := false
	e = fenced(s.DB, *first, func(tx *gorm.DB) error { called = true; return nil })
	if e == nil || called {
		t.Fatal("stale fenced write accepted")
	}
	if e = fenced(s.DB, *second, func(tx *gorm.DB) error { return nil }); e != nil {
		t.Fatal("current fence rejected", e)
	}
}
func checkLiveFileGuards(t *testing.T, s *Service) {
	t.Helper()
	tenant := newID()
	root, e := createFolder(s.DB, tenant, "", "root", "")
	if e != nil {
		t.Fatal(e)
	}
	managed, e := createFolder(s.DB, tenant, root, "skill", "skill_space")
	if e != nil {
		t.Fatal(e)
	}
	legacy, e := createFolder(s.DB, tenant, managed, "ordinary-child", "")
	if e != nil {
		t.Fatal(e)
	}
	defer s.DB.Where("tenant_id = ?", tenant).Delete(&entity.File{})
	old := dao.DB
	dao.DB = s.DB
	defer func() { dao.DB = old }()
	for _, id := range []string{managed, legacy} {
		if e := dao.GuardSkillFile(id); !errors.Is(e, dao.ErrSkillManagedFile) {
			t.Fatal("managed ancestor guard missing", e)
		}
	}
	files, total, e := dao.NewFileDAO().GetByPfID(tenant, root, 1, 20, "name", false, "")
	if e != nil || total != 0 || len(files) != 0 {
		t.Fatal("list or total leaked", e)
	}
	fs := service.NewFileService()
	if ok, _ := fs.DeleteFiles(context.Background(), tenant, []string{root}); ok {
		t.Fatal("ancestor delete bypass")
	}
	if ok, _ := fs.MoveFiles(tenant, []string{managed}, root, ""); ok {
		t.Fatal("managed move bypass")
	}
	if ok, _ := fs.MoveFiles(tenant, []string{root}, managed, ""); ok {
		t.Fatal("managed destination bypass")
	}
	if _, e = fs.UploadFile(tenant, managed, nil); !errors.Is(e, dao.ErrSkillManagedFile) {
		t.Fatal("upload bypass")
	}
	if _, e = fs.CreateFolder(tenant, "bad", managed, "folder"); !errors.Is(e, dao.ErrSkillManagedFile) {
		t.Fatal("folder bypass")
	}
	h := handler.NewFileHandler(fs, nil)
	r := gin.New()
	r.Use(func(c *gin.Context) { c.Set("user", &entity.User{ID: tenant}) })
	r.GET("/files", h.ListFiles)
	r.GET("/files/:id", h.Download)
	for _, path := range []string{"/files?parent_id=" + managed, "/files/" + legacy} {
		recorder := httptest.NewRecorder()
		r.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, path, nil))
		if recorder.Code != 404 {
			t.Fatalf("generic Files bypass status=%d", recorder.Code)
		}
	}
}

// Recovery cannot discard queued work, but terminal operations cannot pin orphan generations forever.
func checkLiveOrphanGenerations(t *testing.T, s *Service) {
	t.Helper()
	tenant := newID()
	sp, e := s.CreateSpace(context.Background(), tenant, "orphan fixture", "")
	if e != nil {
		t.Fatal(e)
	}
	defer func() {
		for _, model := range []any{&Operation{}, &Generation{}, &Config{}, &Space{}, &entity.File{}} {
			if e := s.DB.Where("tenant_id = ?", tenant).Delete(model).Error; e != nil {
				t.Error(e)
			}
		}
	}()
	gs := []Generation{}
	for i := 0; i < 2; i++ {
		id := newID()
		g := Generation{ID: id, TenantID: tenant, SpaceID: sp.ID, IndexName: "skill_" + id, State: "building", Config: defaultConfig(tenant, sp.ID).Snapshot(), ConfigRevision: 1, SourceRevision: 1, Times: nowTimes()}
		if e := s.DB.Create(&g).Error; e != nil {
			t.Fatal(e)
		}
		gs = append(gs, g)
	}
	op := makeOperation(tenant, "reindex", newID(), JSON{"generation_id": gs[0].ID})
	if e := s.DB.Create(&op).Error; e != nil {
		t.Fatal(e)
	}
	recovery := *s
	recovery.Index = nil // Check durable recovery before any external cleanup.
	recovery.cleanRetired(context.Background())
	for i, g := range gs {
		var got Generation
		if e := s.DB.First(&got, "id = ?", g.ID).Error; e != nil {
			t.Fatal(e)
		}
		want := "failed"
		if i == 0 {
			want = "building"
		}
		if got.State != want {
			t.Fatalf("orphan recovery state=%s want=%s", got.State, want)
		}
	}
}
