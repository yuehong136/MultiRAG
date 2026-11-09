package dao

import (
	"database/sql"
	"errors"
	"gorm.io/gorm"

	"testing"
	"time"

	"multirag/internal/entity"
	"multirag/internal/testsupport/scratch"
)

type timeProbe struct {
	ID   string `gorm:"primaryKey"`
	Name string `gorm:"check:time_probe_valid,name <> 'invalid'"`
	entity.BaseModel
}

func (timeProbe) TableName() string { return "t_ai_time_probe" }

type timeRow struct {
	create, update   sql.NullInt64
	created, updated sql.NullTime
	name             string
}

// Exercises actual GORM callbacks with independent SQL readback in an owned database.
func TestBaseModelTimeScratchMySQL(t *testing.T) {
	f := scratch.New(t)
	db, readback := f.DB, f.Read
	previous := DB
	DB = db
	t.Cleanup(func() { DB = previous })
	// Start from the pre-hook settings shape with populated historical values.
	if _, err := readback.Exec(`CREATE TABLE IF NOT EXISTS system_settings (
		name varchar(128) PRIMARY KEY, source varchar(32) NOT NULL,
		data_type varchar(32) NOT NULL, value text NOT NULL,
		create_time bigint, create_date datetime(0), update_time bigint, update_date datetime(0))`); err != nil {
		t.Fatal(err)
	}
	if _, err := readback.Exec(`INSERT INTO system_settings(name,source,data_type,value,create_time,update_time)
		VALUES ('legacy','old','string','retained',1700000000,1700000000123)
		`); err != nil {
		t.Fatal(err)
	}
	if err := db.AutoMigrate(&timeProbe{}, &entity.SystemSettings{}); err != nil {
		t.Fatal(err)
	}
	var legacyCreate, legacyUpdate int64
	if err := readback.QueryRow("SELECT create_time,update_time FROM system_settings WHERE name='legacy'").Scan(&legacyCreate, &legacyUpdate); err != nil {
		t.Fatal(err)
	}
	if legacyCreate != 1700000000 || legacyUpdate != 1700000000123 {
		t.Fatal("migration rewrote historical timestamps")
	}
	for _, table := range []string{"t_ai_time_probe", "system_settings"} {
		if err := db.Exec("DELETE FROM " + table).Error; err != nil {
			t.Fatal(err)
		}
	}
	read := func(id string) timeRow {
		t.Helper()
		var row timeRow
		if err := readback.QueryRow("SELECT name,create_time,update_time,create_date,update_date FROM t_ai_time_probe WHERE id=?", id).Scan(&row.name, &row.create, &row.update, &row.created, &row.updated); err != nil {
			t.Fatal(err)
		}
		return row
	}
	checkNow := func(stamp sql.NullInt64, date sql.NullTime, before time.Time) {
		t.Helper()
		if !stamp.Valid || !date.Valid || stamp.Int64 < before.UnixMilli() || stamp.Int64 > time.Now().UnixMilli() || date.Time.Nanosecond() != 0 || date.Time.Unix() != stamp.Int64/1000 {
			t.Fatalf("epoch millisecond and second date mismatch: %v %v", stamp, date)
		}
	}
	before := time.Now()
	record := timeProbe{ID: "automatic", Name: "initial"}
	if err := db.Create(&record).Error; err != nil {
		t.Fatal(err)
	}
	initial := read(record.ID)
	checkNow(initial.create, initial.created, before)
	checkNow(initial.update, initial.updated, before)
	if initial.create != initial.update || !initial.created.Time.Equal(initial.updated.Time) {
		t.Fatal("creation timestamps diverged")
	}
	creationUnchanged := func(row timeRow) {
		t.Helper()
		if row.create != initial.create || !row.created.Time.Equal(initial.created.Time) {
			t.Fatal("creation timestamps changed")
		}
	}
	reset := func() time.Time {
		t.Helper()
		if _, err := readback.Exec("UPDATE t_ai_time_probe SET update_time=0,update_date=? WHERE id=?", time.Unix(0, 0), record.ID); err != nil {
			t.Fatal(err)
		}
		return time.Now()
	}
	t.Run("map-auto", func(t *testing.T) {
		before := reset()
		if err := db.Model(&timeProbe{}).Where("id=?", record.ID).Updates(map[string]any{"name": "map"}).Error; err != nil {
			t.Fatal(err)
		}
		row := read(record.ID)
		checkNow(row.update, row.updated, before)
		creationUnchanged(row)
	})
	for _, key := range []string{"update_time", "UpdateTime"} {
		t.Run("map-explicit-"+key, func(t *testing.T) {
			for _, stamp := range []any{nil, int64(0), int64(1700000000), int64(1700000000123)} {
				date := time.Unix(1700000000, 0).UTC()
				updates := map[string]any{key: stamp, "UpdateDate": date}
				if err := db.Model(&timeProbe{}).Where("id=?", record.ID).Updates(updates).Error; err != nil {
					t.Fatal(err)
				}
				row := read(record.ID)
				creationUnchanged(row)
				if (stamp == nil && row.update.Valid) || (stamp != nil && (!row.update.Valid || row.update.Int64 != stamp.(int64))) || !row.updated.Time.Equal(date) {
					t.Fatal("explicit timestamp was replaced")
				}
			}
		})
	}
	t.Run("struct-auto-and-explicit", func(t *testing.T) {
		before := reset()
		if err := db.Model(&timeProbe{}).Where("id=?", record.ID).Updates(timeProbe{Name: "struct"}).Error; err != nil {
			t.Fatal(err)
		}
		checkNow(read(record.ID).update, read(record.ID).updated, before)
		stamp := int64(0)
		date := time.Unix(0, 0).UTC()
		if err := db.Model(&timeProbe{}).Where("id=?", record.ID).Updates(timeProbe{Name: "explicit-struct", BaseModel: entity.BaseModel{UpdateTime: &stamp, UpdateDate: &date}}).Error; err != nil {
			t.Fatal(err)
		}
		row := read(record.ID)
		if !row.update.Valid || row.update.Int64 != 0 || !row.updated.Time.Equal(date) {
			t.Fatal("explicit struct timestamps changed")
		}
		creationUnchanged(row)
	})
	t.Run("save-refreshes-loaded-time", func(t *testing.T) {
		before := reset()
		var loaded timeProbe
		if err := db.First(&loaded, "id=?", record.ID).Error; err != nil {
			t.Fatal(err)
		}
		loaded.Name = "saved"
		if err := db.Save(&loaded).Error; err != nil {
			t.Fatal(err)
		}
		row := read(record.ID)
		checkNow(row.update, row.updated, before)
		creationUnchanged(row)
	})
	t.Run("select-omit", func(t *testing.T) {
		reset()
		old := read(record.ID)
		if err := db.Model(&timeProbe{}).Where("id=?", record.ID).Select("name").Updates(map[string]any{"name": "selected"}).Error; err != nil {
			t.Fatal(err)
		}
		if row := read(record.ID); row.name != "selected" || row.update != old.update || !row.updated.Time.Equal(old.updated.Time) {
			t.Fatal("Select contract changed")
		}
		if err := db.Model(&timeProbe{}).Where("id=?", record.ID).Omit("UpdateTime", "update_date").Updates(map[string]any{"name": "omitted"}).Error; err != nil {
			t.Fatal(err)
		}
		if row := read(record.ID); row.name != "omitted" || row.update != old.update || !row.updated.Time.Equal(old.updated.Time) {
			t.Fatal("Omit contract changed")
		}
		before := reset()
		if err := db.Model(&timeProbe{}).Where("id=?", record.ID).Select("name", "UpdateTime", "UpdateDate").Updates(timeProbe{Name: "selected-time"}).Error; err != nil {
			t.Fatal(err)
		}
		row := read(record.ID)
		checkNow(row.update, row.updated, before)
		creationUnchanged(row)
	})
	t.Run("batch-and-explicit-creation", func(t *testing.T) {
		zero := int64(0)
		date := time.Unix(1700000000, 0).UTC()
		batch := []timeProbe{{ID: "batch-auto", Name: "auto"}, {ID: "batch-explicit", Name: "explicit", BaseModel: entity.BaseModel{CreateTime: &zero, UpdateTime: &zero, CreateDate: &date, UpdateDate: &date}}}
		before := time.Now()
		if err := db.Create(&batch).Error; err != nil {
			t.Fatal(err)
		}
		row := read("batch-auto")
		checkNow(row.create, row.created, before)
		row = read("batch-explicit")
		if row.create.Int64 != 0 || row.update.Int64 != 0 || !row.created.Time.Equal(date) || !row.updated.Time.Equal(date) {
			t.Fatal("explicit creation timestamps replaced")
		}
		before = time.Now()
		if err := db.Model(&timeProbe{}).Where("id IN ?", []string{"batch-auto", "batch-explicit"}).Updates(map[string]any{"name": "bulk"}).Error; err != nil {
			t.Fatal(err)
		}
		for _, id := range []string{"batch-auto", "batch-explicit"} {
			row := read(id)
			checkNow(row.update, row.updated, before)
			if row.name != "bulk" {
				t.Fatal("bulk update lost")
			}
		}
	})
	t.Run("failure-rollback", func(t *testing.T) {
		old := read(record.ID)
		if err := db.Model(&timeProbe{}).Where("id=?", record.ID).Updates(map[string]any{"name": "invalid"}).Error; err == nil {
			t.Fatal("constraint failure required")
		}
		if row := read(record.ID); row != old {
			t.Fatal("failed update changed SQL")
		}
		if err := db.Transaction(func(tx *gorm.DB) error {
			if err := tx.Model(&timeProbe{}).Where("id=?", record.ID).Update("name", "rolled-back").Error; err != nil {
				return err
			}
			return errors.New("owned rollback")
		}); err == nil {
			t.Fatal("rollback error required")
		}
		if row := read(record.ID); row != old {
			t.Fatal("rolled-back update changed SQL")
		}
	})
	t.Run("system-settings", func(t *testing.T) {
		settings := NewSystemSettingsDAO()
		if err := settings.SaveOrCreate("owned", "one", "initial", "string"); err != nil {
			t.Fatal(err)
		}
		var create, update int64
		var created, updated time.Time
		var value, source, typ string
		check := func() {
			t.Helper()
			if err := readback.QueryRow("SELECT value,source,data_type,create_time,update_time,create_date,update_date FROM system_settings WHERE name='owned'").Scan(&value, &source, &typ, &create, &update, &created, &updated); err != nil {
				t.Fatal(err)
			}
		}
		check()
		original := create
		before := time.Now()
		if err := settings.SaveOrCreate("owned", "two", "revised", "number"); err != nil {
			t.Fatal(err)
		}
		check()
		if value != "two" || source != "revised" || typ != "number" || create != original {
			t.Fatal("settings metadata or creation changed")
		}
		checkNow(sql.NullInt64{Int64: update, Valid: true}, sql.NullTime{Time: updated, Valid: true}, before)
		if err := settings.Transaction(func(tx *gorm.DB) error {
			if err := settings.CreateWithTx(tx, &entity.SystemSettings{Name: "tx", Value: "x", Source: "local", DataType: "string"}); err != nil {
				return err
			}
			return settings.UpdateByNameWithTx(tx, "tx", &entity.SystemSettings{Value: "y", Source: "updated", DataType: "number"})
		}); err != nil {
			t.Fatal(err)
		}
		if err := readback.QueryRow("SELECT value,source,data_type,create_time,update_time,create_date,update_date FROM system_settings WHERE name='tx'").Scan(&value, &source, &typ, &create, &update, &created, &updated); err != nil {
			t.Fatal(err)
		}
		if value != "y" || source != "updated" || typ != "number" {
			t.Fatal("transaction settings lost")
		}
		checkNow(sql.NullInt64{Int64: update, Valid: true}, sql.NullTime{Time: updated, Valid: true}, before)
	})
}
