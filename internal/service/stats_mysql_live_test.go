package service

import (
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/testsupport/scratch"
	"testing"
	"time"
)

func TestBatchGMySQLStats(t *testing.T) {
	f := scratch.New(t)
	old := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = old })
	if err := f.DB.AutoMigrate(&entity.UserTenant{}, &entity.Tenant{}, &entity.User{}); err != nil {
		t.Fatal(err)
	}
	// Match the canonical SQL table names used by the conversation statistics query.
	for _, statement := range []string{"CREATE TABLE dialog(id varchar(32) PRIMARY KEY,tenant_id varchar(32) NOT NULL)", "CREATE TABLE api_4_conversation(id varchar(32) PRIMARY KEY,dialog_id varchar(32),user_id varchar(32),source varchar(16) NULL,tokens bigint,duration double,`round` bigint,thumb_up bigint,create_date datetime)"} {
		if err := f.DB.Exec(statement).Error; err != nil {
			t.Fatal(err)
		}
	}
	active := "1"
	day := time.Date(2026, 10, 20, 12, 0, 0, 0, time.Local)
	for _, id := range []string{"owner", "other"} {
		if err := f.DB.Create(&entity.Tenant{ID: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := f.DB.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: id, Role: "owner", Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := f.DB.Exec("INSERT INTO dialog(id,tenant_id) VALUES (?,?)", id, id).Error; err != nil {
			t.Fatal(err)
		}
		if err := f.DB.Exec("INSERT INTO api_4_conversation(id,dialog_id,user_id,tokens,duration,`round`,thumb_up,create_date) VALUES (?,?,?,1000,0,0,0,?)", id, id, id, day).Error; err != nil {
			t.Fatal(err)
		}
	}
	result, err := NewSystemService().GetStats("owner", day.Add(-12*time.Hour).Format("2006-01-02 15:04:05"), day.Add(12*time.Hour).Format("2006-01-02 15:04:05"), nil)
	if err != nil || len(result.PV) != 1 || result.PV[0][1] != int64(1) || result.Tokens[0][1] != float64(1) || result.Speed[0][1] != float64(10000) {
		t.Fatal("canonical tenant/daily/zero denominator stats", result, err)
	}
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM api_4_conversation").Scan(&count); err != nil || count != 2 {
		t.Fatal("read-only stats changed SQL rows", count, err)
	}
	f.Journal.Event("canonical-stats-readback", map[string]any{"rows": count, "scoped_pv": 1, "zero_duration_formula": 10000})
}
