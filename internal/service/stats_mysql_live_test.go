package service

import (
	"database/sql"
	"encoding/json"
	mysqlDriver "github.com/go-sql-driver/mysql"
	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/dao"
	"multirag/internal/testsupport"
	"os"
	"strings"
	"testing"
	"time"
)

func TestBatchGMySQLStats(t *testing.T) {
	path := os.Getenv("MULTIRAG_BATCH_G_MYSQL")
	if path == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned MySQL required")
		}
		t.Skip("requires owned MySQL")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("private config unavailable")
	}
	var config struct {
		DSN        string
		ServerUUID string
	}
	if json.Unmarshal(raw, &config) != nil {
		t.Fatal("invalid config")
	}
	parsed, err := mysqlDriver.ParseDSN(config.DSN)
	if err != nil || !strings.HasPrefix(parsed.DBName, "multirag_batch_g_") {
		t.Fatal("unowned MySQL")
	}
	caseJournal := testsupport.NewCase(t)
	caseJournal.Event("reserve-mysql-stat-tables", map[string]any{"database": parsed.DBName, "tables": []string{"t_ai_dialogs", "t_ai_api4conversations"}})
	db, err := gorm.Open(mysql.Open(config.DSN), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("connect owned MySQL")
	}
	pool, _ := db.DB()
	defer pool.Close()
	read, err := sql.Open("mysql", config.DSN)
	if err != nil {
		t.Fatal("readback connect")
	}
	defer read.Close()
	var actual string
	if err := read.QueryRow("SELECT @@server_uuid").Scan(&actual); err != nil || actual != config.ServerUUID {
		t.Fatal("wrong MySQL server")
	}
	dao.DB = db
	t.Cleanup(func() { dao.DB = nil })
	// The Go entity's existing jsonb tags do not support a full MySQL startup.
	// This fixture verifies only the persisted column contract used by Stats.
	for _, statement := range []string{
		"CREATE TABLE t_ai_dialogs(id varchar(32) PRIMARY KEY,tenant_id varchar(32) NOT NULL)",
		"CREATE TABLE t_ai_api4conversations(id varchar(32) PRIMARY KEY,dialog_id varchar(32),user_id varchar(32),source varchar(16) NULL,tokens bigint,duration double,`round` bigint,thumb_up bigint,create_date datetime)",
	} {
		if err := db.Exec(statement).Error; err != nil {
			t.Fatal(err)
		}
	}
	day := time.Date(2026, 10, 20, 12, 0, 0, 0, time.UTC)
	for _, id := range []string{"stats-owner", "stats-other"} {
		if err := db.Exec("INSERT INTO t_ai_dialogs(id,tenant_id) VALUES (?,?)", id, id).Error; err != nil {
			t.Fatal(err)
		}
		if err := db.Exec("INSERT INTO t_ai_api4conversations(id,dialog_id,user_id,tokens,duration,`round`,thumb_up,create_date) VALUES (?,?,?,1000,0,0,0,?)", id, id, id, day).Error; err != nil {
			t.Fatal(err)
		}
	}
	result, code, err := NewSystemService().GetStats(t.Context(), "stats-owner", day.Add(-12*time.Hour), day.Add(12*time.Hour), nil)
	if err != nil || code != 0 || len(result.PV) != 1 || result.PV[0][1] != int64(1) || result.Tokens[0][1] != float64(1) || result.Speed[0][1] != float64(0) {
		t.Fatal("MySQL daily/tenant/zero stats", code, err)
	}
	var n int
	if err := read.QueryRow("SELECT count(*) FROM t_ai_api4conversations").Scan(&n); err != nil || n != 2 {
		t.Fatal("read-only stats changed stored rows", n, err)
	}
	caseJournal.Event("mysql-stat-readback", map[string]any{"rows": n, "scopedPV": 1, "zeroDurationSpeed": 0})
}
