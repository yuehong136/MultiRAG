package dao

import (
	"gorm.io/gorm"
	"multirag/internal/entity"
	"testing"
)

// Frozen target uses ordinary provider/name indexes, not a local identity migration.
func checkModelInstanceCredentials(t *testing.T, db *gorm.DB) {
	t.Helper()
	if err := db.AutoMigrate(&entity.TenantModelInstance{}); err != nil {
		t.Fatal(err)
	}
	original := &entity.TenantModelInstance{ID: "legacy", ProviderID: "provider", InstanceName: "legacy", APIKey: "retained", Status: "inactive", Extra: `{"region":"office","value":0}`}
	if err := db.Create(original).Error; err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"keyless-a", "keyless-b"} {
		if err := db.Create(&entity.TenantModelInstance{ID: id, ProviderID: id, InstanceName: "default", APIKey: "", Status: "active"}).Error; err != nil {
			t.Fatal("keyless instance rejected", err)
		}
	}
	shared := *original
	shared.ID = "shared"
	shared.InstanceName = "another-endpoint"
	if err := db.Create(&shared).Error; err != nil {
		t.Fatal("shared credential rejected", err)
	}
	if err := db.AutoMigrate(&entity.TenantModelInstance{}); err != nil {
		t.Fatal(err)
	}
	var actual entity.TenantModelInstance
	if err := db.First(&actual, "id=?", original.ID).Error; err != nil || actual.APIKey != original.APIKey || actual.Extra != original.Extra || actual.Status != original.Status {
		t.Fatal("migration changed persisted credential row", err)
	}
	duplicate := *original
	if err := db.Create(&duplicate).Error; err == nil {
		t.Fatal("duplicate primary key accepted")
	}
}
