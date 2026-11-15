package handler

import (
	"errors"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/entity"
	"multirag/internal/server"
	"multirag/internal/service"
	"multirag/internal/storage"
)

type failingHandlerStorage struct{ storage.Storage }

func (s *failingHandlerStorage) Get(string, string, ...string) ([]byte, error) {
	return nil, errors.New("password=private-marker host=private-backend")
}
func TestHandlerInternalErrorsSQLHTTP(t *testing.T) {
	f := newCanonicalHTTPFixture(t)
	doc := f.document(t, "shared", "source.txt")
	location := "source.txt"
	if err := f.DB.Model(&entity.Document{}).Where("id=?", doc).Update("location", location).Error; err != nil {
		t.Fatal(err)
	}
	factory := storage.GetStorageFactory()
	previous := factory.GetStorage()
	factory.SetStorage(&failingHandlerStorage{Storage: f.Store})
	t.Cleanup(func() { factory.SetStorage(previous) })
	h := NewDocumentHandler(service.NewDocumentService(), service.NewDatasetService())
	app := f.app()
	app.GET("/documents/:id/preview", h.GetDocumentPreview)
	app.GET("/datasets/:dataset_id/documents/:document_id", h.DownloadDocument)
	url := f.serve(t, app)
	for _, path := range []string{"/documents/" + doc + "/preview", "/datasets/" + f.datasets["shared"] + "/documents/" + doc} {
		_, result, raw, _ := f.request(t, url, "owner", "GET", path, nil)
		if result["code"] == float64(0) || result["data"] != nil || strings.Contains(string(raw), "private-") {
			t.Fatal("internal source error exposed or became success", string(raw))
		}
	}
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM document WHERE id=? AND location=?", doc, location).Scan(&count); err != nil || count != 1 {
		t.Fatal("read error mutated source SQL", count, err)
	}
	f.Journal.Event("canonical-source-error-readback", map[string]any{"actual_mysql_http": true, "controlled_storage_failure": true, "error_opaque_zero_sql_writes": true})
}
func TestPublicConfigsRedactsSecretsWithoutMutatingSource(t *testing.T) {
	f := newCanonicalHTTPFixture(t)
	cfg := server.GetConfig()
	originalDatabase, originalSecret := cfg.Database.Password, cfg.Server.SecretKey
	secret := "private-marker"
	cfg.Database.Password = secret
	cfg.Server.SecretKey = &secret
	t.Cleanup(func() { cfg.Database.Password = originalDatabase; cfg.Server.SecretKey = originalSecret })
	app := gin.New()
	app.GET("/configs", NewSystemHandler(service.NewSystemService()).GetConfigs)
	url := f.serve(t, app)
	status, result, raw, _ := f.request(t, url, "", "GET", "/configs", nil)
	if status != 200 || result["code"] != float64(0) || strings.Contains(string(raw), secret) {
		t.Fatal("public system config disclosed secret", string(raw))
	}
	if cfg.Database.Password != secret || cfg.Server.SecretKey == nil || *cfg.Server.SecretKey != secret {
		t.Fatal("redaction modified runtime source configuration")
	}
}
