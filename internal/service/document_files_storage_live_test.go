package service

import (
	"bytes"
	"multirag/internal/entity"
	"multirag/internal/storage"
	"testing"
)

func TestDocumentFilesMinioReadback(t *testing.T) {
	f := canonicalLink(t)
	if err := f.service.convertFiles([]string{f.one}, []string{f.a}, f.owner); err != nil {
		t.Fatal(err)
	}
	doc := canonicalLinkedDocs(t, f, f.a)[0]
	reader := &canonicalDocumentReadStore{Storage: f.Store}
	storage.GetStorageFactory().SetStorage(reader)
	result, err := f.service.documentSvc.DownloadDocument(f.a, doc.ID)
	if err != nil || !bytes.Equal(result.Data, []byte("abc")) || reader.reads != 1 {
		t.Fatal(result, err, reader.reads)
	}
	before := reader.reads
	if result, err := f.service.documentSvc.DownloadDocument(f.b, doc.ID); err == nil || result != nil || reader.reads != before {
		t.Fatal("substituted dataset reached source object", result, err, reader.reads)
	}
	if err := f.DB.Model(&entity.File{}).Where("id=?", f.one).Update("location", "../escape").Error; err != nil {
		t.Fatal(err)
	}
	if result, err := f.service.documentSvc.DownloadDocument(f.a, doc.ID); err == nil || result != nil || reader.reads != before {
		t.Fatal("unsafe registered address reached storage", result, err, reader.reads)
	}
	location := f.one + ".txt"
	if err := f.DB.Model(&entity.File{}).Where("id=?", f.one).Update("location", location).Error; err != nil {
		t.Fatal(err)
	}
	if err := f.Store.Put(f.folder, location, []byte{}); err != nil {
		t.Fatal(err)
	}
	result, err = f.service.documentSvc.DownloadDocument(f.a, doc.ID)
	if err != nil || result.Data == nil || len(result.Data) != 0 {
		t.Fatal("actual empty object treated as missing", result, err)
	}
	artifactBucket := f.Prefix + "artifacts"
	t.Setenv("SANDBOX_ARTIFACT_BUCKET", artifactBucket)
	if err := f.Store.Put(artifactBucket, "safe.svg", []byte("<svg>controlled artifact</svg>")); err != nil {
		t.Fatal(err)
	}
	artifact, err := f.service.documentSvc.GetDocumentArtifact("safe.svg")
	if err != nil || !artifact.ForceAttachment || artifact.ContentType != "image/svg+xml" {
		t.Fatal(artifact, err)
	}
	f.ReadObject(t, artifactBucket, "safe.svg", artifact.Data)
	f.Journal.Event("canonical-document-download-readback", map[string]any{"cross_dataset_denied_before_read": true, "empty_bytes_preserved": true, "artifact_forced_attachment": true, "artifact_tenant_ownership_provided_by_frozen_target": false})
}
