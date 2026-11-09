package admin

import (
	"encoding/base64"
	"multirag/internal/entity"
	"testing"
)

func TestInitDefaultAdminAtomicSQL(t *testing.T) {
	f := newAdminFixture(t)
	if err := f.DB.Where("1=1").Delete(&entity.User{}).Error; err != nil {
		t.Fatal(err)
	}
	if err := f.DB.Exec("CREATE TRIGGER reject_default_admin_membership BEFORE INSERT ON user_tenant FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='controlled bootstrap failure'").Error; err != nil {
		t.Fatal(err)
	}
	if err := f.service.InitDefaultAdmin(); err == nil {
		t.Fatal("bootstrap partial failure acknowledged")
	}
	for _, table := range []string{"user", "tenant", "user_tenant"} {
		var count int
		if err := f.Read.QueryRow("SELECT count(*) FROM " + table).Scan(&count); err != nil || count != 0 {
			t.Fatal("bootstrap left partial rows", table, count, err)
		}
	}
	if err := f.DB.Exec("DROP TRIGGER reject_default_admin_membership").Error; err != nil {
		t.Fatal(err)
	}
	if err := f.service.InitDefaultAdmin(); err != nil {
		t.Fatal(err)
	}
	if err := f.service.InitDefaultAdmin(); err != nil {
		t.Fatal("idempotent bootstrap", err)
	}
	var userID, password, relationID, tenantID string
	if err := f.Read.QueryRow("SELECT id,password FROM user WHERE email=?", "admin@ragflow.io").Scan(&userID, &password); err != nil || !CheckWerkzeugPassword(base64.StdEncoding.EncodeToString([]byte("admin")), password) {
		t.Fatal("target bootstrap password/hash lost", err)
	}
	if err := f.Read.QueryRow("SELECT id,tenant_id FROM user_tenant WHERE user_id=?", userID).Scan(&relationID, &tenantID); err != nil || relationID == "" || tenantID != userID {
		t.Fatal("bootstrap owner membership absent", err)
	}
}
