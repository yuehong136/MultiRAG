package handler

import (
	"multirag/internal/entity"
	"multirag/internal/service"
	"multirag/internal/storage"
	"multirag/internal/utility"
	"testing"
)

func TestFileCommitLiveAcceptance(t *testing.T) {
	f := newCanonicalHTTPFixture(t)
	if err := f.DB.AutoMigrate(&entity.FileCommit{}, &entity.FileCommitItem{}); err != nil {
		t.Fatal(err)
	}
	prior := storage.GetStorageFactory().GetStorage()
	storage.GetStorageFactory().SetStorage(f.Store)
	t.Cleanup(func() { storage.GetStorageFactory().SetStorage(prior) })
	folder, file := utility.GenerateToken(), utility.GenerateToken()
	for _, row := range []entity.File{{ID: folder, ParentID: folder, TenantID: f.ids["owner"], CreatedBy: f.ids["owner"], Name: "root", Type: "folder"}, {ID: file, ParentID: folder, TenantID: f.ids["owner"], CreatedBy: f.ids["owner"], Name: "file.txt", Type: "file"}} {
		if err := f.DB.Create(&row).Error; err != nil {
			t.Fatal(err)
		}
	}
	svc := service.NewFileCommitService()
	h := NewFileCommitHandler(svc)
	app := f.app()
	app.POST("/folders/:folder_id/commits", h.CreateCommit)
	app.GET("/folders/:folder_id/commits/:commit_id", h.GetCommit)
	url := f.serve(t, app)
	body := map[string]any{"message": "canonical commit", "files": []any{map[string]any{"file_id": file, "file_name": "file.txt", "operation": "add", "content": "first source"}}}
	for _, actor := range []string{"", "foreign", "invite", "revoked", "disabled"} {
		_, result, _, _ := f.request(t, url, actor, "POST", "/folders/"+folder+"/commits", body)
		if result["code"] == float64(0) {
			t.Fatal("unauthorized commit", actor, result)
		}
	}
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM file_commit").Scan(&count); err != nil || count != 0 || len(f.ObjectKeys(t)) != 0 {
		t.Fatal("denied commit had SQL/object side effects", count, err)
	}
	_, created, _, _ := f.request(t, url, "owner", "POST", "/folders/"+folder+"/commits", body)
	if created["code"] != float64(0) {
		t.Fatal("original commit creation", created)
	}
	data := created["data"].(map[string]any)
	id := data["id"].(string)
	content, err := svc.GetCommitFileContent(folder, id, file)
	if err != nil || string(content) != "first source" {
		t.Fatal("real object snapshot", string(content), err)
	}
	if _, err := svc.GetCommitFileContent("foreign", id, file); err == nil {
		t.Fatal("substituted workspace exposed snapshot")
	}
	for _, actor := range []string{"foreign", "invite", "revoked", "disabled"} {
		_, result, _, _ := f.request(t, url, actor, "GET", "/folders/"+folder+"/commits/"+id, nil)
		if result["code"] == float64(0) || result["data"] != nil {
			t.Fatal("foreign snapshot detail", actor, result)
		}
	}
	if err := f.Read.QueryRow("SELECT count(*) FROM file_commit WHERE id=? AND folder_id=? AND author_id=?", id, folder, f.ids["owner"]).Scan(&count); err != nil || count != 1 {
		t.Fatal("independent commit SQL readback", count, err)
	}
	f.Journal.Event("canonical-file-commit-http-readback", map[string]any{"http_mysql_minio": true, "foreign_invite_revoked_disabled_zero_mutations": true, "workspace_bound_content": true, "failure_compensation_suite": "service/TestFileCommitCleanupAttemptsAllReservedObjects"})
}
