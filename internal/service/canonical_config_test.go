package service

import (
	"github.com/google/uuid"
	"multirag/internal/server"
	"multirag/internal/testsupport"
	"os"
	"path/filepath"
	"testing"
)

func canonicalServiceConfig(t *testing.T) {
	t.Helper()
	if server.GetConfig() != nil {
		return
	}
	j := testsupport.NewCase(t)
	directory := filepath.Join(os.TempDir(), "canonical-service-config-"+uuid.NewString())
	j.Event("reserve-empty-service-config", map[string]any{"path": directory})
	if err := os.Mkdir(directory, 0700); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(directory); err != nil {
			t.Error(err)
		}
		_, err := os.Lstat(directory)
		j.Event("cleanup-empty-service-config", map[string]any{"path": directory, "absent": os.IsNotExist(err)})
		if !os.IsNotExist(err) {
			t.Error("service configuration fixture survived")
		}
	})
	path := filepath.Join(directory, "service.json")
	if err := os.WriteFile(path, []byte(`{}`), 0600); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(path); err != nil {
		t.Fatal(err)
	}
}
