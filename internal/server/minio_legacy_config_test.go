package server

import (
	"os"
	"path/filepath"
	"testing"
)

func TestMinioZeroLegacyConfiguration(t *testing.T) {
	previous, previousViper := globalConfig, globalViper
	t.Cleanup(func() { globalConfig, globalViper = previous, previousViper })
	for _, test := range []struct{ config, host, region string }{
		{"minio_0:\n  host: legacy:9000\n  region: legacy-region\n", "legacy:9000", "legacy-region"},
		{"minio:\n  host: normal:9000\n  region: normal-region\nminio_0:\n  host: legacy:9000\n", "normal:9000", "normal-region"},
		{"storage_engine:\n  type: minio\n  minio:\n    host: explicit:9000\n    region: explicit-region\nminio_0:\n  host: legacy:9000\n", "explicit:9000", "explicit-region"},
	} {
		globalConfig = nil
		path := filepath.Join(t.TempDir(), "service.yaml")
		if err := os.WriteFile(path, []byte(test.config), 0600); err != nil {
			t.Fatal(err)
		}
		if err := FromConfigFile(path); err != nil {
			t.Fatal(err)
		}
		if globalConfig.StorageEngine.Minio == nil || globalConfig.StorageEngine.Minio.Host != test.host || globalConfig.StorageEngine.Minio.Region != test.region {
			t.Fatal("MinIO legacy precedence lost", globalConfig.StorageEngine.Minio)
		}
	}
}
