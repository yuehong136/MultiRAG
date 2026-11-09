package dao

import (
	"multirag/internal/testsupport/scratch"
	"testing"
)

func TestBatchGMySQLCredentials(t *testing.T) {
	f := scratch.New(t)
	old := DB
	DB = f.DB
	t.Cleanup(func() { DB = old })
	checkModelInstanceCredentials(t, f.DB)
	var key, status, extra string
	if err := f.Read.QueryRow("SELECT api_key,status,extra FROM tenant_model_instance WHERE id=?", "legacy").Scan(&key, &status, &extra); err != nil || key != "retained" || status != "inactive" || extra != `{"region":"office","value":0}` {
		t.Fatal("independent persisted credential readback mismatch", err)
	}
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM tenant_model_instance").Scan(&count); err != nil || count != 4 {
		t.Fatal("failed duplicate write changed rows", count, err)
	}
}
