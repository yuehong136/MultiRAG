package dao

import (
	"database/sql"
	"encoding/json"
	"os"
	"strings"
	"testing"

	mysqlDriver "github.com/go-sql-driver/mysql"
	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/entity"
	"multirag/internal/testsupport"
)

// TestBatchGMySQLCredentials owns a dedicated database supplied by the registered harness.
func TestBatchGMySQLCredentials(t *testing.T) {
	path := os.Getenv("MULTIRAG_BATCH_G_MYSQL")
	if path == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned MySQL config required")
		}
		t.Skip("requires owned MySQL config")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("private config unavailable")
	}
	var config struct {
		DSN        string
		ServerUUID string
	}
	if json.Unmarshal(data, &config) != nil {
		t.Fatal("invalid private config")
	}
	parsed, err := mysqlDriver.ParseDSN(config.DSN)
	if err != nil || !strings.HasPrefix(parsed.DBName, "multirag_batch_g_") {
		t.Fatal("unowned MySQL database")
	}
	journal := testsupport.NewCase(t)
	journal.Event("before-schema", map[string]any{"database": parsed.DBName})
	db, err := gorm.Open(mysql.Open(config.DSN), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent), TranslateError: true})
	if err != nil {
		t.Fatal("owned MySQL connection failed")
	}
	pool, err := db.DB()
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	read, err := sql.Open("mysql", config.DSN)
	if err != nil {
		t.Fatal("independent MySQL readback connection failed")
	}
	defer read.Close()
	var actualUUID string
	if err := read.QueryRow("SELECT @@server_uuid").Scan(&actualUUID); err != nil || actualUUID != config.ServerUUID {
		t.Fatal("wrong MySQL server")
	}
	checkModelInstanceCredentialMigration(t, db)
	// DDL on MySQL is not transactional. A conflicting old schema must be rejected
	// before changing either index; the independent connection reads rows/indexes.
	if err := db.Exec("DELETE FROM tenant_model_instance").Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Exec("DROP INDEX idx_tenant_model_instance_provider_name ON tenant_model_instance").Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Exec("CREATE UNIQUE INDEX idx_tenant_model_instance_api_key ON tenant_model_instance(api_key)").Error; err != nil {
		t.Fatal(err)
	}
	for _, row := range []entity.TenantModelInstance{{ID: "conflict-a", ProviderID: "same-provider", InstanceName: "same", APIKey: "key-a", Status: "inactive", Extra: `{"zero":0}`}, {ID: "conflict-b", ProviderID: "same-provider", InstanceName: "same", APIKey: "key-b", Status: "active", Extra: `{"zero":0}`}} {
		if err := db.Create(&row).Error; err != nil {
			t.Fatal(err)
		}
	}
	before := mysqlCredentialSnapshot(t, read)
	if err := migrateModelInstanceCredentials(db); err == nil || !strings.Contains(err.Error(), "duplicate model instance names") {
		t.Fatal("missing conflict diagnosis")
	}
	if mysqlCredentialSnapshot(t, read) != before || !db.Migrator().HasIndex(&entity.TenantModelInstance{}, "idx_tenant_model_instance_api_key") || db.Migrator().HasIndex(&entity.TenantModelInstance{}, "idx_tenant_model_instance_provider_name") {
		t.Fatal("MySQL conflicting DDL changed rows or indexes")
	}
	journal.Event("mysql-conflict-readback", map[string]any{"rowsAndOldIndexUnchanged": true, "newIndexAbsent": true})
}
func mysqlCredentialSnapshot(t *testing.T, db *sql.DB) string {
	t.Helper()
	rows, err := db.Query("SELECT id,provider_id,instance_name,api_key,status,extra FROM tenant_model_instance ORDER BY id")
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	result := [][]string{}
	for rows.Next() {
		row := make([]string, 6)
		if err := rows.Scan(&row[0], &row[1], &row[2], &row[3], &row[4], &row[5]); err != nil {
			t.Fatal(err)
		}
		result = append(result, row)
	}
	if rows.Err() != nil {
		t.Fatal(rows.Err())
	}
	data, _ := json.Marshal(result)
	return string(data)
}
