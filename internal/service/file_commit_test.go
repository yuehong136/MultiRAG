package service

import (
	"errors"
	"gorm.io/gorm"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/storage"
	"multirag/internal/testsupport/scratch"
	"testing"
)

func TestFileCommitCleanupAttemptsAllReservedObjects(t *testing.T) {
	for _, scenario := range []string{"success", "nil-storage", "foreign-author", "foreign-file", "late-sql-failure", "put-failure"} {
		t.Run(scenario, func(t *testing.T) {
			f := scratch.New(t)
			previousDB, previousStore := dao.DB, storage.GetStorageFactory().GetStorage()
			dao.DB = f.DB
			storage.GetStorageFactory().SetStorage(f.Store)
			t.Cleanup(func() { dao.DB = previousDB; storage.GetStorageFactory().SetStorage(previousStore) })
			if err := f.DB.AutoMigrate(&entity.File{}, &entity.FileCommit{}, &entity.FileCommitItem{}); err != nil {
				t.Fatal(err)
			}
			owner, folder, file := f.Prefix+"owner", f.Prefix+"folder", f.Prefix+"file"
			for _, row := range []entity.File{{ID: folder, ParentID: folder, TenantID: owner, CreatedBy: owner, Name: "root", Type: FileTypeFolder}, {ID: file, ParentID: folder, TenantID: owner, CreatedBy: owner, Name: "file.txt", Type: "file"}} {
				if err := f.DB.Create(&row).Error; err != nil {
					t.Fatal(err)
				}
			}
			changes := []entity.FileChange{{FileID: file, FileName: "file.txt", Operation: "add", Content: "actual committed contents"}}
			switch scenario {
			case "nil-storage":
				storage.GetStorageFactory().SetStorage(nil)
			case "foreign-author":
				owner = "foreign"
			case "foreign-file":
				if err := f.DB.Model(&entity.File{}).Where("id = ?", file).Update("tenant_id", "foreign").Error; err != nil {
					t.Fatal(err)
				}
			case "late-sql-failure":
				if err := f.DB.Callback().Create().Before("gorm:create").Register("commit-fail-item", func(tx *gorm.DB) {
					if tx.Statement.Schema != nil && tx.Statement.Schema.Name == "FileCommitItem" {
						tx.AddError(errors.New("controlled SQL item failure"))
					}
				}); err != nil {
					t.Fatal(err)
				}
				t.Cleanup(func() { f.DB.Callback().Create().Remove("commit-fail-item") })
			case "put-failure":
				f.Store.PutFailure = true
			}
			commit, err := NewFileCommitService().CreateCommit(folder, owner, "actual commit", changes)
			var commits, items int
			if e := f.Read.QueryRow("SELECT COUNT(*) FROM file_commit").Scan(&commits); e != nil {
				t.Fatal(e)
			}
			if e := f.Read.QueryRow("SELECT COUNT(*) FROM file_commit_item").Scan(&items); e != nil {
				t.Fatal(e)
			}
			keys := f.ObjectKeys(t)
			if scenario == "success" {
				if err != nil || commit == nil || commits != 1 || items != 1 || len(keys) != 1 {
					t.Fatal(commit, err, commits, items, keys)
				}
				data, readErr := NewFileCommitService().GetCommitFileContent(folder, commit.ID, file)
				if readErr != nil || string(data) != "actual committed contents" {
					t.Fatal(string(data), readErr)
				}
				if _, err := NewFileCommitService().GetCommitFileContent("foreign", commit.ID, file); err == nil {
					t.Fatal("substituted workspace accepted")
				}
			} else {
				if err == nil || commit != nil || commits != 0 || items != 0 || len(keys) != 0 {
					t.Fatal("failed commit leaked", commit, err, commits, items, keys)
				}
			}
		})
	}
}
