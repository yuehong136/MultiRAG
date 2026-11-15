package service

import (
	"strings"
	"testing"

	"github.com/google/uuid"
	"multirag/internal/common"
	"multirag/internal/entity"
)

func TestCreateDatasetCanBeReadBackWithUUID1(t *testing.T) {
	f := canonicalLink(t)
	if err := f.DB.AutoMigrate(&entity.Connector{}, &entity.Connector2Kb{}); err != nil {
		t.Fatal(err)
	}
	service := NewDatasetService()
	created, code, err := service.CreateDataset(&CreateDatasetRequest{Name: f.Prefix + "created"}, f.owner)
	if err != nil || code != common.CodeSuccess {
		t.Fatal("create dataset", created, code, err)
	}
	id, ok := created["id"].(string)
	if !ok || len(id) != 32 || strings.Contains(id, "-") {
		t.Fatal("canonical dataset ID", created)
	}
	parsed, err := uuid.Parse(id)
	if err != nil || parsed.Version() != 1 {
		t.Fatal("created dataset UUID1", id, err)
	}
	var stored string
	if err := f.Read.QueryRow("SELECT id FROM knowledgebase WHERE id=? AND tenant_id=?", id, f.owner).Scan(&stored); err != nil || stored != id {
		t.Fatal("independent created ID readback", stored, err)
	}
	got, code, err := service.GetDataset(id, f.owner)
	if err != nil || code != common.CodeSuccess || got["id"] != id || got["name"] != created["name"] {
		t.Fatal("created dataset cannot be read", got, code, err)
	}
	if got, _, err := service.GetDataset(strings.ReplaceAll(uuid.NewString(), "-", ""), f.owner); err == nil || got != nil {
		t.Fatal("UUID4 validation was relaxed", got, err)
	}
	f.Journal.Event("created-dataset-uuid1-readback", map[string]any{"create_then_get": true, "independent_sql_readback": true, "uuid4_still_rejected": true})
}
