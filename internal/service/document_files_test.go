package service

import (
	"errors"
	"multirag/internal/entity"
	"multirag/internal/storage"
	"testing"
)

type canonicalDocumentReadStore struct {
	storage.Storage
	data        []byte
	failure     error
	reads       int
	exists      bool
	bucket, key string
}

func (s *canonicalDocumentReadStore) Get(bucket, key string, tenant ...string) ([]byte, error) {
	s.reads++
	s.bucket, s.key = bucket, key
	if s.Storage != nil {
		return s.Storage.Get(bucket, key, tenant...)
	}
	return s.data, s.failure
}
func (s *canonicalDocumentReadStore) ObjExist(bucket, key string, tenant ...string) bool {
	if s.Storage != nil {
		return s.Storage.ObjExist(bucket, key, tenant...)
	}
	return s.exists
}
func TestDocumentFileStorageAddressRejectsTraversal(t *testing.T) {
	index := &canonicalDocumentReadStore{data: []byte("image")}
	previous := storage.GetStorageFactory().GetStorage()
	storage.GetStorageFactory().SetStorage(index)
	t.Cleanup(func() { storage.GetStorageFactory().SetStorage(previous) })
	service := &DocumentService{}
	for _, id := range []string{"", "bucket-../source", "bucket-/source", "../bucket-source", "bucket-dir/../source", "bucket-dir\\source", "bucket-source\x00"} {
		if _, err := service.GetDocumentImage(id); err == nil {
			t.Error("unsafe image address accepted", id)
		}
	}
	if index.reads != 0 {
		t.Fatal("unsafe image address reached storage")
	}
	data, err := service.GetDocumentImage("bucket-nested/source.png")
	if err != nil || string(data) != "image" || index.bucket != "bucket" || index.key != "nested/source.png" {
		t.Fatal(data, err, index.bucket, index.key)
	}
	if _, _, err := service.GetDocumentStorageAddress((*entity.Document)(nil)); err == nil {
		t.Fatal("nil document address accepted")
	}
}
func TestDocumentFileReadObjectPreservesActualStorageFailure(t *testing.T) {
	failure := errors.New("controlled document read failure")
	index := &canonicalDocumentReadStore{failure: failure}
	previous := storage.GetStorageFactory().GetStorage()
	storage.GetStorageFactory().SetStorage(index)
	t.Cleanup(func() { storage.GetStorageFactory().SetStorage(previous) })
	data, err := (&DocumentService{}).GetDocumentImage("bucket-source")
	if data != nil || !errors.Is(err, failure) {
		t.Fatal(data, err)
	}
}
func TestDocumentArtifactValidationRejectsBeforeAnyRead(t *testing.T) {
	index := &canonicalDocumentReadStore{data: []byte("artifact"), exists: true}
	previous := storage.GetStorageFactory().GetStorage()
	storage.GetStorageFactory().SetStorage(index)
	t.Cleanup(func() { storage.GetStorageFactory().SetStorage(previous) })
	service := &DocumentService{}
	for _, filename := range []string{"../file.png", "/file.png", "path\\file.png", "file.exe", ""} {
		if artifact, err := service.GetDocumentArtifact(filename); err == nil || artifact != nil {
			t.Fatal("invalid artifact reached public basename route", filename)
		}
	}
	if index.reads != 0 {
		t.Fatal("invalid artifact performed object read")
	}
	for filename, wantType := range map[string]string{"safe.svg": "image/svg+xml", "safe.html": "text/html", "safe.pdf": "application/pdf"} {
		artifact, err := service.GetDocumentArtifact(filename)
		if err != nil || artifact.ContentType != wantType || artifact.ForceAttachment != (filename != "safe.pdf") {
			t.Fatal(filename, artifact, err)
		}
	}
}
