package service

import (
	"context"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"strings"
	"testing"
)

type skillTestDriver struct {
	models.ModelDriver
	query   bool
	vectors [][]float64
}

func (d *skillTestDriver) Encode(_ *string, _ []string, _ *models.APIConfig, cfg *models.EmbeddingConfig) ([][]float64, error) {
	d.query = cfg.Query
	return d.vectors, nil
}
func TestSkillQueryEmbeddingContract(t *testing.T) {
	d := &skillTestDriver{vectors: [][]float64{{1, 0.1}}}
	bound := &skillEmbeddingModel{EmbeddingModel: models.NewEmbeddingModel(d, nil, &models.APIConfig{})}
	if _, e := bound.Encode(context.Background(), []string{"query"}, true); e != nil || !d.query {
		t.Fatalf("query contract: %v", e)
	}
	if _, e := bound.Encode(context.Background(), []string{"document"}, false); e != nil || d.query {
		t.Fatalf("document contract: %v", e)
	}
	d.vectors = [][]float64{{0, 0}}
	if _, e := bound.Encode(context.Background(), []string{"bad"}, false); e == nil {
		t.Fatal("zero embedding accepted")
	}
}
func TestSkillDirectorySelectionAndPaths(t *testing.T) {
	s := NewSkillIndexerService()
	rows := []*entity.File{{Name: "1.9.0", Type: "folder"}, {Name: "2.0.0", Type: "folder"}, {Name: "3x.0.0", Type: "folder"}, {Name: "not-semver", Type: "folder"}}
	if got := s.findLatestVersion(rows); got == nil || got.Name != "2.0.0" {
		t.Fatalf("wrong highest version: %v", got)
	}
	for _, path := range []string{"../bad", "/absolute", "a//b", "a/./b", "a\\b"} {
		if ValidateSkillUploadPath(path) == nil {
			t.Errorf("accepted %q", path)
		}
	}
	if ValidateSkillUploadPath("nested/中文.txt") != nil {
		t.Fatal("valid relative path rejected")
	}
	name, description, tags := parseCoreMetadata("---\nname: orange\ndescription: |\n  multiline\n  description\ntags: [fruit]\n---\nbody", "fallback")
	if name != "orange" || description != "multiline\ndescription\n" || len(tags) != 1 {
		t.Fatalf("metadata %q %q %v", name, description, tags)
	}
}

func TestSkillNumericVersionDoesNotOverflowOrAcceptSigns(t *testing.T) {
	indexer := NewSkillIndexerService()
	versions := []*entity.File{{Name: "+999.0.0", Type: "folder"}, {Name: "2.0.0", Type: "folder"}, {Name: "99999999999999999999999999999999.0.0", Type: "folder"}}
	got := indexer.findLatestVersion(versions)
	if got == nil || got.Name != versions[2].Name {
		t.Fatal("numeric version ordering changed")
	}
	if indexer.findLatestVersion(versions[:1]) != nil {
		t.Fatal("signed version accepted")
	}
	if ValidateSkillUploadPath(strings.Repeat("中", 255)) != nil || ValidateSkillUploadPath(strings.Repeat("中", 256)) == nil {
		t.Fatal("Unicode segment boundary changed")
	}
}
