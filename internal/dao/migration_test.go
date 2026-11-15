package dao

import (
	"multirag/internal/entity"
	"multirag/internal/testsupport/scratch"
	"testing"
)

func TestMigrationsMySQLSchemaIsolationAndReadback(t *testing.T) {
	foreign := scratch.New(t)
	if err := foreign.DB.AutoMigrate(&entity.TenantLLM{}, &entity.User{}); err != nil {
		t.Fatal(err)
	}
	// A second database with the same tables must not satisfy this database's checks.
	f := scratch.New(t)
	for _, sql := range []string{
		"CREATE TABLE tenant_llm (tenant_id varchar(32) NOT NULL,llm_factory varchar(128) NOT NULL,llm_name varchar(128) NOT NULL,api_key varchar(128),PRIMARY KEY(tenant_id,llm_factory,llm_name))",
		"INSERT INTO tenant_llm VALUES ('tenant','factory','legacy','retained-credential')",
		"CREATE TABLE user (id varchar(32) PRIMARY KEY,email varchar(128))",
		"INSERT INTO user VALUES('legacy','retained@fixture.invalid')",
		"CREATE TABLE task (id varchar(32) PRIMARY KEY,process_duation bigint)",
		"INSERT INTO task VALUES('legacy',123)",
	} {
		if err := f.DB.Exec(sql).Error; err != nil {
			t.Fatal(err)
		}
	}
	for range 2 {
		if err := RunMigrations(f.DB); err != nil {
			t.Fatal(err)
		}
	}
	var id int64
	var key string
	if err := f.Read.QueryRow("SELECT id,api_key FROM tenant_llm WHERE tenant_id='tenant'").Scan(&id, &key); err != nil || id <= 0 || key != "retained-credential" {
		t.Fatal("legacy credential migration lost data", err)
	}
	var duration int64
	if err := f.Read.QueryRow("SELECT process_duration FROM task WHERE id='legacy'").Scan(&duration); err != nil || duration != 123 {
		t.Fatal("renamed duration lost", err)
	}
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM information_schema.statistics WHERE table_schema=? AND table_name='user' AND index_name='idx_user_email_unique'", f.Database).Scan(&count); err != nil || count != 1 {
		t.Fatal("foreign schema hid missing unique email index", count, err)
	}
	if err := f.DB.Exec("INSERT INTO user VALUES('duplicate','retained@fixture.invalid')").Error; err == nil {
		t.Fatal("email unique contract missing")
	}
	for _, table := range []string{"skill_search_configs", "skill_spaces"} {
		if !f.DB.Migrator().HasTable(table) {
			t.Fatal("target skill table missing", table)
		}
	}
	if err := foreign.Read.QueryRow("SELECT count(*) FROM tenant_llm").Scan(&count); err != nil || count != 0 {
		t.Fatal("migration escaped owned database", err)
	}
}
func TestMigrationFailuresAreNotSuccess(t *testing.T) {
	f := scratch.New(t)
	if err := f.DB.Exec("CREATE TABLE user(id varchar(32) PRIMARY KEY,email varchar(128))").Error; err != nil {
		t.Fatal(err)
	}
	if err := f.DB.Exec("INSERT INTO user VALUES('a','same@fixture.invalid'),('b','same@fixture.invalid')").Error; err != nil {
		t.Fatal(err)
	}
	if err := migrateAddUniqueEmail(f.DB); err == nil {
		t.Fatal("duplicate-index migration falsely succeeded")
	}
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM user").Scan(&count); err != nil || count != 2 {
		t.Fatal("failed migration changed rows", err)
	}
	if err := f.DB.Exec("CREATE TABLE dialog(id varchar(32) PRIMARY KEY,top_k varchar(128))").Error; err != nil {
		t.Fatal(err)
	}
	if err := f.DB.Exec("INSERT INTO dialog VALUES('invalid','not-a-number')").Error; err != nil {
		t.Fatal(err)
	}
	if err := modifyColumnTypes(f.DB); err == nil {
		t.Fatal("failed ALTER falsely succeeded")
	}
	var value string
	if err := f.Read.QueryRow("SELECT top_k FROM dialog WHERE id='invalid'").Scan(&value); err != nil || value != "not-a-number" {
		t.Fatal("failed ALTER changed values", err)
	}
}
