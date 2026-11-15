package service

import (
	"bytes"
	"errors"
	"gorm.io/gorm"
	"io"
	"mime/multipart"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/storage"
	"multirag/internal/testsupport/scratch"
	"testing"
)

func canonicalFileHeaders(t *testing.T, names ...string) []*multipart.FileHeader {
	t.Helper()
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	for _, name := range names {
		part, err := writer.CreateFormFile("file", name)
		if err != nil {
			t.Fatal(err)
		}
		io.WriteString(part, "actual uploaded contents")
	}
	writer.Close()
	form, err := multipart.NewReader(&body, writer.Boundary()).ReadForm(1 << 20)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := form.RemoveAll(); err != nil {
			t.Error(err)
		}
	})
	return form.File["file"]
}

type canonicalUploadFailureStore struct {
	storage.Storage
	readErr, removeErr error
}

func (s *canonicalUploadFailureStore) Get(bucket, key string, tenant ...string) ([]byte, error) {
	if s.readErr != nil {
		return nil, s.readErr
	}
	return s.Storage.Get(bucket, key, tenant...)
}
func (s *canonicalUploadFailureStore) Remove(bucket, key string, tenant ...string) error {
	err := s.Storage.Remove(bucket, key, tenant...)
	if err != nil {
		return err
	}
	return s.removeErr
}

func TestCanonicalFileUploadSQLObjectsAndCompensation(t *testing.T) {
	for _, scenario := range []string{"success", "invalid-batch", "cross-tenant", "persisted-put-failure", "readback-failure", "late-sql-failure", "cleanup-error"} {
		t.Run(scenario, func(t *testing.T) {
			f := scratch.New(t)
			oldDB, oldStore := dao.DB, storage.GetStorageFactory().GetStorage()
			dao.DB = f.DB
			storage.GetStorageFactory().SetStorage(f.Store)
			t.Cleanup(func() { dao.DB = oldDB; storage.GetStorageFactory().SetStorage(oldStore) })
			if err := f.DB.AutoMigrate(&entity.File{}); err != nil {
				t.Fatal(err)
			}
			owner, parent := f.Prefix+"owner", f.Prefix+"folder"
			if err := f.DB.Create(&entity.File{ID: parent, ParentID: parent, TenantID: owner, CreatedBy: owner, Name: "/", Type: FileTypeFolder}).Error; err != nil {
				t.Fatal(err)
			}
			headers := canonicalFileHeaders(t, "same.txt", "same.txt")
			first := errors.New("controlled upload failure")
			cleanup := errors.New("controlled cleanup failure")
			switch scenario {
			case "invalid-batch":
				headers[1].Filename = "../escape"
			case "cross-tenant":
				owner = "foreign"
			case "persisted-put-failure":
				f.Store.PutFailure = true
			case "readback-failure":
				storage.GetStorageFactory().SetStorage(&canonicalUploadFailureStore{Storage: f.Store, readErr: first})
			case "cleanup-error":
				storage.GetStorageFactory().SetStorage(&canonicalUploadFailureStore{Storage: f.Store, readErr: first, removeErr: cleanup})
			case "late-sql-failure":
				calls := 0
				if err := f.DB.Callback().Create().Before("gorm:create").Register("consumer-fail-second", func(tx *gorm.DB) {
					if tx.Statement.Schema != nil && tx.Statement.Schema.Name == "File" {
						calls++
						if calls == 2 {
							tx.AddError(first)
						}
					}
				}); err != nil {
					t.Fatal(err)
				}
				t.Cleanup(func() { f.DB.Callback().Create().Remove("consumer-fail-second") })
			}
			result, err := NewFileService().UploadFile(owner, parent, headers)
			var count int
			if e := f.Read.QueryRow("SELECT COUNT(*) FROM file WHERE id <> ?", parent).Scan(&count); e != nil {
				t.Fatal(e)
			}
			keys := f.ObjectKeys(t)
			if scenario == "success" {
				if err != nil || len(result) != 2 || count != 2 || len(keys) != 2 {
					t.Fatalf("upload=%v err=%v SQL=%d objects=%v", result, err, count, keys)
				}
				if result[0]["location"] == result[1]["location"] {
					t.Fatal("duplicate name overwrote object")
				}
				for _, row := range result {
					f.ReadObject(t, parent, row["location"].(string), []byte("actual uploaded contents"))
				}
			} else {
				if err == nil || result != nil || count != 0 || len(keys) != 0 {
					t.Fatalf("failed upload leaked: %v err=%v SQL=%d objects=%v", result, err, count, keys)
				}
				if scenario == "cleanup-error" && (!errors.Is(err, first) || !errors.Is(err, cleanup)) {
					t.Fatal("cleanup hid first error", err)
				}
				if (scenario == "invalid-batch" || scenario == "cross-tenant") && f.Store.Calls != 0 {
					t.Fatal("invalid batch reached storage")
				}
			}
		})
	}
}

func TestCanonicalFileUploadPreservesMultipartDirectory(t *testing.T) {
	f := scratch.New(t)
	oldDB, oldStore := dao.DB, storage.GetStorageFactory().GetStorage()
	dao.DB = f.DB
	storage.GetStorageFactory().SetStorage(f.Store)
	t.Cleanup(func() { dao.DB = oldDB; storage.GetStorageFactory().SetStorage(oldStore) })
	if err := f.DB.AutoMigrate(&entity.File{}); err != nil {
		t.Fatal(err)
	}
	owner, parent := f.Prefix+"owner", f.Prefix+"root"
	if err := f.DB.Create(&entity.File{ID: parent, ParentID: parent, TenantID: owner, CreatedBy: owner, Name: "/", Type: FileTypeFolder}).Error; err != nil {
		t.Fatal(err)
	}
	files := canonicalFileHeaders(t, "one/中文.txt", "two/中文.txt")
	result, err := NewFileService().UploadFile(owner, parent, files)
	if err != nil || len(result) != 2 {
		t.Fatal("nested multipart upload", result, err)
	}
	rows, err := f.Read.Query("SELECT d.name,p.name,d.location,d.parent_id FROM file d JOIN file p ON p.id=d.parent_id WHERE d.type<>? ORDER BY p.name", FileTypeFolder)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	count := 0
	for rows.Next() {
		var name, folder, location, bucket string
		if err := rows.Scan(&name, &folder, &location, &bucket); err != nil {
			t.Fatal(err)
		}
		if name != "中文.txt" || folder != []string{"one", "two"}[count] {
			t.Fatal("multipart path flattened", name, folder)
		}
		f.ReadObject(t, bucket, location, []byte("actual uploaded contents"))
		count++
	}
	if err := rows.Err(); err != nil || count != 2 {
		t.Fatal(count, err)
	}
}
