package dao

import (
	"errors"
	"testing"

	mysqlDriver "github.com/go-sql-driver/mysql"
	"github.com/jackc/pgx/v5/pgconn"
	"gorm.io/gorm"
	"multirag/internal/entity"
)

// Called inside the owned PostgreSQL fixture; also usable with an owned MySQL fixture.
func checkModelInstanceCredentialMigration(t *testing.T, db *gorm.DB) {
	t.Helper()
	model := &entity.TenantModelInstance{}
	if err := db.AutoMigrate(model); err != nil {
		t.Fatal(err)
	}
	legacy := &entity.TenantModelInstance{ID: "credentials-legacy", ProviderID: "provider", InstanceName: "legacy", APIKey: "preserve-nonempty", Status: "inactive", Extra: `{"region":"office","value":0}`}
	if err := db.Create(legacy).Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Exec("CREATE UNIQUE INDEX idx_tenant_model_instance_api_key ON tenant_model_instance(api_key)").Error; err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if err := migrateModelInstanceCredentials(db); err != nil {
			t.Fatal("old schema/idempotent upgrade", err)
		}
	}
	for _, id := range []string{"credentials-local-a", "credentials-local-b"} {
		if err := db.Create(&entity.TenantModelInstance{ID: id, ProviderID: id, InstanceName: "default", APIKey: "", Status: "active"}).Error; err != nil {
			t.Fatal("keyless instances conflict", err)
		}
	}
	duplicate := *legacy
	duplicate.ID = "credentials-duplicate"
	duplicate.APIKey = "different-key"
	err := db.Create(&duplicate).Error
	var pgErr *pgconn.PgError
	var mysqlErr *mysqlDriver.MySQLError
	if !errors.Is(err, gorm.ErrDuplicatedKey) && !(errors.As(err, &pgErr) && pgErr.Code == "23505") && !(errors.As(err, &mysqlErr) && mysqlErr.Number == 1062) {
		t.Fatal("expected provider/name identity violation", err)
	}
	// Repeat AutoMigrate as startup does, then repeat the manual migration.
	if err := db.AutoMigrate(model); err != nil {
		t.Fatal(err)
	}
	if err := migrateModelInstanceCredentials(db); err != nil {
		t.Fatal(err)
	}
	if db.Migrator().HasIndex(model, "idx_tenant_model_instance_api_key") || db.Migrator().HasIndex(model, "idx_tenant_model_instance_api_key_nonempty") || !db.Migrator().HasIndex(model, "idx_tenant_model_instance_provider_name") {
		t.Fatal("provider/name identity index missing or global credential index survived")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	var key, status, extra string
	if err := pool.QueryRow("SELECT api_key,status,extra FROM tenant_model_instance WHERE id='credentials-legacy'").Scan(&key, &status, &extra); err != nil || key != legacy.APIKey || status != legacy.Status || extra != legacy.Extra {
		t.Fatal("legacy row changed", status, extra, err)
	}
	var count int
	if err := pool.QueryRow("SELECT count(*) FROM tenant_model_instance WHERE id LIKE 'credentials-%'").Scan(&count); err != nil || count != 3 {
		t.Fatal("unique denial wrote rows", count, err)
	}
	shared := *legacy
	shared.ID = "credentials-shared"
	shared.InstanceName = "another-endpoint"
	if err := db.Create(&shared).Error; err != nil {
		t.Fatal("same credential at distinct endpoint identity rejected", err)
	}
	t.Log("old credential uniqueness migrated idempotently to provider/name identity; keyless/shared credentials preserved; duplicate identity denied; independent legacy readback unchanged")
}
