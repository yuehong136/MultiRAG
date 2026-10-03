package dao

import (
	"encoding/json"
	"net/url"
	"os"
	"strings"
	"testing"

	"multirag/internal/entity"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
)

// Run only against a separately created, empty scratch database.
func TestTenantModelExtraScratchPostgres(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_GO_TENANT_MODEL_DSN")
	if dsn == "" {
		t.Skip("requires an owned tenant-model scratch database")
	}
	parsed, err := url.Parse(dsn)
	if err != nil || !strings.HasPrefix(strings.TrimPrefix(parsed.Path, "/"), "multirag_go_tenant_model_") {
		t.Fatal("scratch DB name is not owned")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("scratch database connection failed")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	oldDB := DB
	DB = db
	t.Cleanup(func() { DB = oldDB })
	if db.Migrator().HasTable(&entity.TenantModel{}) || db.Migrator().HasTable(&entity.TenantModelInstance{}) {
		t.Fatal("scratch database already contains tenant-model tables")
	}

	// Migrate a populated pre-Extra table through the same helper used by InitDB.
	if err := db.Exec(`CREATE TABLE tenant_model (
		id varchar(32) PRIMARY KEY, model_name varchar(128),
		provider_id varchar(32) NOT NULL, instance_id varchar(32) NOT NULL,
		model_type varchar(32) NOT NULL, status varchar(32) DEFAULT 'active')`).Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Exec(`INSERT INTO tenant_model (id, model_name, provider_id, instance_id, model_type, status)
		VALUES ('legacy', 'existing', 'provider', 'instance', 'chat', 'disabled')`).Error; err != nil {
		t.Fatal(err)
	}
	for _, model := range []any{&entity.TenantModel{}, &entity.TenantModelInstance{}} {
		if err := autoMigrateSafely(db, model); err != nil {
			t.Fatal(err)
		}
	}
	modelDAO, instanceDAO := NewTenantModelDAO(), NewTenantModelInstanceDAO()
	legacy, err := modelDAO.GetByID("legacy")
	if err != nil || legacy.Extra != "{}" || legacy.ModelName != "existing" || legacy.Status != "disabled" || legacy.ModelType != "chat" {
		t.Fatalf("legacy row was not preserved: model=%+v, error=%v", legacy, err)
	}

	for _, test := range []struct {
		table string
		size  int
	}{
		{table: "tenant_model", size: 1024},
		{table: "tenant_model_instance", size: 512},
	} {
		t.Run(test.table, func(t *testing.T) {
			var size int
			var defaultValue string
			if err := pool.QueryRow(`SELECT character_maximum_length, column_default
				FROM information_schema.columns WHERE table_schema = current_schema()
				AND table_name = $1 AND column_name = 'extra'`, test.table).Scan(&size, &defaultValue); err != nil {
				t.Fatal(err)
			}
			if size != test.size || !strings.Contains(defaultValue, "'{}'") {
				t.Fatalf("physical extra column: size=%d, default=%q", size, defaultValue)
			}
			insert := `INSERT INTO tenant_model (id, provider_id, instance_id, model_type)
				VALUES ('sql-default', 'provider', 'sql-default', 'chat')`
			if test.table == "tenant_model_instance" {
				insert = `INSERT INTO tenant_model_instance (id, provider_id, instance_name, api_key)
					VALUES ('sql-default', 'provider', 'sql-default', 'fixture-sql-default')`
			}
			if _, err := pool.Exec(insert); err != nil {
				t.Fatal(err)
			}
			var sqlDefault string
			if err := pool.QueryRow("SELECT extra FROM " + test.table + " WHERE id = 'sql-default'").Scan(&sqlDefault); err != nil || sqlDefault != "{}" {
				t.Fatalf("SQL insert default: extra=%q, error=%v", sqlDefault, err)
			}

			for _, extra := range []string{"", `{"region":"global","label":"中文","options":{"enabled":false}}`} {
				id := "default"
				if extra != "" {
					id = "custom"
				}
				want := extra
				if want == "" {
					want = "{}"
				}
				var got any
				if test.table == "tenant_model" {
					model := &entity.TenantModel{ID: id, ModelName: id, ProviderID: "provider", InstanceID: id, ModelType: "chat", Extra: extra}
					if err := modelDAO.Create(model); err != nil {
						t.Fatal(err)
					}
					got, err = modelDAO.GetByID(id)
					if err != nil {
						t.Fatal(err)
					}
					byName, err := modelDAO.GetModelByProviderIDAndInstanceIDAndModelName("provider", id, id)
					if err != nil || byName.Extra != want {
						t.Fatalf("model name lookup: model=%+v, error=%v", byName, err)
					}
					models, err := modelDAO.GetModelsByInstanceID(id)
					if err != nil || len(models) != 1 || models[0].Extra != want {
						t.Fatalf("model list: models=%+v, error=%v", models, err)
					}
				} else {
					instance := &entity.TenantModelInstance{ID: id, InstanceName: id, ProviderID: id, APIKey: "fixture-" + id, Extra: extra}
					if err := instanceDAO.Create(instance); err != nil {
						t.Fatal(err)
					}
					got, err = instanceDAO.GetByID(id)
					if err != nil {
						t.Fatal(err)
					}
					byName, err := instanceDAO.GetByProviderIDAndInstanceName(id, id)
					if err != nil || byName.Extra != want {
						t.Fatalf("instance name lookup: instance=%+v, error=%v", byName, err)
					}
					instances, err := instanceDAO.GetAllInstancesByProviderID(id)
					if err != nil || len(instances) != 1 || instances[0].Extra != want {
						t.Fatalf("instance list: instances=%+v, error=%v", instances, err)
					}
				}
				var stored string
				if err := pool.QueryRow("SELECT extra FROM "+test.table+" WHERE id = $1", id).Scan(&stored); err != nil || stored != want {
					t.Fatalf("independent SQL readback: extra=%q, error=%v", stored, err)
				}
				data, err := json.Marshal(got)
				if err != nil {
					t.Fatal(err)
				}
				var fields map[string]json.RawMessage
				if err := json.Unmarshal(data, &fields); err != nil {
					t.Fatal(err)
				}
				var encodedExtra string
				if err := json.Unmarshal(fields["extra"], &encodedExtra); err != nil || encodedExtra != want {
					t.Fatalf("JSON extra mapping: extra=%q, error=%v", encodedExtra, err)
				}
			}
		})
	}
}
