//go:build filecommitintegration

package handler

import (
	"context"
	"encoding/json"
	"errors"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"syscall"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"go.uber.org/zap"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/service"
	"multirag/internal/storage"
)

type fileCommitLiveConfig struct {
	DSN, User, Other, Tenant, Folder, File, First string
	Minio                                         server.MinioConfig
}

func fileCommitRecord(t *testing.T, event map[string]any) {
	t.Helper()
	path := os.Getenv("MULTIRAG_RUN_JOURNAL")
	if path == "" {
		t.Fatal("registered journal required")
	}
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	event["event"] = "file-commit-case"
	event["pid"] = os.Getpid()
	event["pgid"] = syscall.Getpgrp()
	b, _ := json.Marshal(event)
	if _, err := f.Write(append(b, '\n')); err != nil {
		t.Fatal(err)
	}
	if err := f.Sync(); err != nil {
		t.Fatal(err)
	}
}

type commitLiveStore struct {
	*storage.MinioStorage
	t                 *testing.T
	fail              bool
	failWorking       bool
	failRemoveWorking bool
	written           [][2]string
	bucket            string
}

func (s *commitLiveStore) Put(bucket, key string, data []byte, tenant ...string) error {
	fileCommitRecord(s.t, map[string]any{"action": "reserve-object", "bucket": s.bucket, "key": bucket + "/" + key})
	s.written = append(s.written, [2]string{bucket, key})
	if err := s.MinioStorage.Put(bucket, key, data, tenant...); err != nil {
		return err
	}
	if s.fail || (s.failWorking && strings.HasPrefix(key, ".file-working/")) {
		return errors.New("injected after real put")
	}
	return nil
}
func (s *commitLiveStore) RemoveContext(ctx context.Context, bucket, key string, tenant ...string) error {
	if s.failRemoveWorking && strings.HasPrefix(key, ".file-working/") {
		return errors.New("injected working removal failure")
	}
	return s.MinioStorage.RemoveContext(ctx, bucket, key, tenant...)
}

func TestFileCommitLiveAcceptance(t *testing.T) {
	cfgPath := os.Getenv("MULTIRAG_FILE_COMMIT_CONFIG")
	if cfgPath == "" {
		t.Fatal("live config required")
	}
	var cfg fileCommitLiveConfig
	b, err := os.ReadFile(cfgPath)
	if err != nil {
		t.Fatal(err)
	}
	if err = json.Unmarshal(b, &cfg); err != nil {
		t.Fatal(err)
	}
	db, err := gorm.Open(postgres.Open(cfg.DSN), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("open scratch database")
	}
	sqlDB, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	defer sqlDB.Close()
	st, err := storage.NewMinioStorage(&cfg.Minio)
	if err != nil {
		t.Fatal("open owned MinIO bucket")
	}
	store := &commitLiveStore{MinioStorage: st, t: t, bucket: cfg.Minio.Bucket}
	oldDB, oldStorage, oldLogger := dao.DB, storage.GetStorageFactory().GetStorage(), common.Logger
	dao.DB = db
	storage.GetStorageFactory().SetStorage(st)
	common.Logger = zap.NewNop()
	defer func() { dao.DB = oldDB; storage.GetStorageFactory().SetStorage(oldStorage); common.Logger = oldLogger }()
	svc := &service.FileCommitService{DB: db, Store: store}
	gin.SetMode(gin.TestMode)
	r := gin.New()
	g := r.Group("/api/v1")
	g.GET("/datasets/:dataset_id", func(c *gin.Context) {})
	g.GET("/files/:id", func(c *gin.Context) {})
	g.Use(func(c *gin.Context) {
		if u := c.GetHeader("X-Test-User"); u != "" {
			c.Set("user", &entity.User{ID: u})
		}
	})
	RegisterFileCommitRoutes(g, NewFileCommitHandler(svc))
	call := func(method, path, user, body string) (int, map[string]any) {
		t.Helper()
		req := httptest.NewRequest(method, "/api/v1"+path, strings.NewReader(body))
		req.Header.Set("X-Test-User", user)
		req.Header.Set("Content-Type", "application/json")
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		var result map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil {
			t.Fatal(w.Body.String())
		}
		return w.Code, result
	}
	base := "/folders/" + cfg.Folder
	for _, suffix := range []string{"/commits", "/commits/" + cfg.First, "/commits/" + cfg.First + "/tree", "/commits/" + cfg.First + "/files/" + cfg.File + "/content", "/commits/diff?from=" + cfg.First + "&to=" + cfg.First, "/changes"} {
		status, result := call("GET", base+suffix, cfg.User, "")
		if status != 200 || result["code"] != float64(0) {
			t.Fatal(status, result)
		}
		status, _ = call("GET", base+suffix, cfg.Other, "")
		if status != 404 {
			t.Fatalf("cross tenant allowed %s %d", suffix, status)
		}
	}
	status, _ := call("GET", base+"/commits", "", "")
	if status != 200 {
		t.Fatal(status)
	} // common unauthenticated business code
	status, _ = call("POST", base+"/commits", cfg.Other, `{"message":"denied","files":[]}`)
	if status != 404 {
		t.Fatal(status)
	}
	ctx := context.Background()
	folder, err := svc.Resolve(ctx, db, "folders", cfg.Folder, cfg.User, false, false)
	if err != nil {
		t.Fatal(err)
	}
	first, err := svc.Get(ctx, folder, cfg.First)
	if err != nil {
		t.Fatal(err)
	}
	if content, err := svc.Content(ctx, first, cfg.File); err != nil || string(content) != "old" {
		t.Fatal("Python snapshot content not readable", err)
	}
	content := "go replacement"
	req := entity.CreateFileCommit{Message: "go replacement", Files: []entity.FileChange{{FileID: cfg.File, Operation: "modify", Content: &content}}, ParentID: &cfg.First, ParentSpecified: true}
	commit, err := svc.Create(ctx, "folders", cfg.Folder, cfg.User, req)
	if err != nil {
		t.Fatal(err)
	}
	store.fail = true
	bad := "bad"
	req.Files[0].Content = &bad
	req.ParentID = &commit.ID
	if _, err := svc.Create(ctx, "folders", cfg.Folder, cfg.User, req); err == nil {
		t.Fatal("ignored storage failure")
	}
	store.fail = false
	var live entity.File
	if err := db.Where("id = ?", cfg.File).First(&live).Error; err != nil || live.Size != int64(len(content)) {
		t.Fatal("SQL not rolled back", err)
	}
	var count int64
	if err := db.Model(&entity.FileCommit{}).Where("folder_id = ?", cfg.Folder).Count(&count).Error; err != nil || count != 2 {
		t.Fatal(count, err)
	}

	// A failed working-object cleanup must not skip the snapshot object.
	checker, err := storage.NewMinioStorage(&cfg.Minio)
	if err != nil {
		t.Fatal(err)
	}
	startObjects := len(store.written)
	store.failWorking = true
	store.failRemoveWorking = true
	req.Files[0].Content = &bad
	req.ParentID = &commit.ID
	if _, err := svc.Create(ctx, "folders", cfg.Folder, cfg.User, req); err == nil {
		t.Fatal("cleanup fault hidden")
	}
	store.failWorking = false
	store.failRemoveWorking = false
	attempts := store.written[startObjects:]
	if len(attempts) != 2 {
		t.Fatal("expected snapshot + working", attempts)
	}
	for _, object := range attempts {
		exists, err := checker.ObjectExistsContext(ctx, object[0], object[1])
		if err != nil {
			t.Fatal(err)
		}
		working := strings.HasPrefix(object[1], ".file-working/")
		if exists != working {
			t.Fatal("later cleanup skipped", object, exists)
		}
		if working {
			if err := st.RemoveContext(ctx, object[0], object[1]); err != nil {
				t.Fatal(err)
			}
			exists, err = checker.ObjectExistsContext(ctx, object[0], object[1])
			if err != nil || exists {
				t.Fatal("precise recovery failed", err)
			}
		}
		fileCommitRecord(t, map[string]any{"action": "cleanup-fault-independent-readback", "bucket": cfg.Minio.Bucket, "key": object[0] + "/" + object[1], "absent": !exists, "workingRecovery": working})
	}
	ordinaryID := strings.ReplaceAll(uuid.NewString(), "-", "")
	ordinaryName, ordinaryBytes := "ordinary.txt", "history survives ordinary Files"
	ordinaryCommit, err := svc.Create(ctx, "folders", cfg.Folder, cfg.User, entity.CreateFileCommit{Message: "ordinary lifecycle", Files: []entity.FileChange{{FileID: ordinaryID, Operation: "add", FileName: &ordinaryName, Content: &ordinaryBytes}}})
	if err != nil {
		t.Fatal(err)
	}
	var ordinary entity.File
	if err := db.Where("id = ?", ordinaryID).First(&ordinary).Error; err != nil {
		t.Fatal(err)
	}
	snap := ordinaryCommit.TreeState[ordinaryID]
	if ordinary.Location == nil || *ordinary.Location == snap.Location || !strings.HasPrefix(*ordinary.Location, ".file-working/") {
		t.Fatal("live snapshot object alias")
	}
	working := *ordinary.Location
	files := service.NewFileService()
	if ok, message := files.MoveFiles(cfg.Tenant, []string{ordinaryID}, live.ParentID, ""); !ok {
		t.Fatal(message)
	}
	if ok, message := files.DeleteFiles(ctx, cfg.Tenant, []string{ordinaryID}); !ok {
		t.Fatal(message)
	}
	if exists, err := st.ObjectExistsContext(ctx, ordinary.ParentID, working); err != nil || exists {
		t.Fatal("working object survived move/delete", err)
	}
	if b, err := svc.Content(ctx, ordinaryCommit, ordinaryID); err != nil || string(b) != ordinaryBytes {
		t.Fatal("ordinary lifecycle destroyed history", err)
	}
	headID := ordinaryCommit.ID
	// Two simultaneous expected-parent writes permit exactly one child.
	var wg sync.WaitGroup
	results := make(chan error, 2)
	for range 2 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := svc.Create(ctx, "folders", cfg.Folder, cfg.User, entity.CreateFileCommit{Message: "concurrent", Files: []entity.FileChange{}, ParentID: &headID, ParentSpecified: true})
			results <- err
		}()
	}
	wg.Wait()
	close(results)
	success, conflict := 0, 0
	for err := range results {
		if err == nil {
			success++
		} else {
			var e *service.FileCommitError
			if errors.As(err, &e) && e.Status == 409 {
				conflict++
			} else {
				t.Fatal(err)
			}
		}
	}
	if success != 1 || conflict != 1 {
		t.Fatal(success, conflict)
	}
	// Deleted live row must leave both Go and Python historical bytes readable.
	deleted, err := svc.Create(ctx, "folders", cfg.Folder, cfg.User, entity.CreateFileCommit{Message: "go delete", Files: []entity.FileChange{{FileID: cfg.File, Operation: "delete"}}})
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := deleted.TreeState[cfg.File]; ok {
		t.Fatal("deleted entry remains active")
	}
	if data, err := svc.Content(ctx, first, cfg.File); err != nil || string(data) != "old" {
		t.Fatal(err)
	}
	history, err := svc.Versions(ctx, cfg.User, cfg.File)
	if err != nil || len(history) != 3 {
		t.Fatal(history, err)
	}
	if _, err := svc.Versions(ctx, cfg.Other, cfg.File); err == nil {
		t.Fatal("cross tenant versions")
	}
	items, err := (dao.FileCommitDAO{DB: db, TenantID: cfg.Tenant, FolderID: cfg.Folder}).Items(ctx, deleted.ID)
	if err != nil || len(items) != 1 || items[0].Operation != "delete" {
		t.Fatal(items, err)
	}
	fileCommitRecord(t, map[string]any{"action": "go-sql-http-object-acceptance", "first": cfg.First, "replacement": commit.ID, "deleted": deleted.ID, "concurrentSuccess": success, "concurrentConflict": conflict})
}
