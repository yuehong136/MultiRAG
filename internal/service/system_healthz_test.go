package service

import (
	"context"
	"encoding/json"
	"multirag/internal/dao"
	rediscache "multirag/internal/engine/redis"
	"multirag/internal/storage"
	"multirag/internal/testsupport/scratch"
	"strings"
	"testing"
	"time"
)

func TestAfter7fbHealthzStates(t *testing.T) {
	f := scratch.New(t)
	oldDB := dao.DB
	oldStore := storage.GetStorageFactory().GetStorage()
	dao.DB = f.DB
	storage.GetStorageFactory().SetStorage(f.Store)
	t.Cleanup(func() { dao.DB = oldDB; storage.GetStorageFactory().SetStorage(oldStore); rediscache.Close() })
	if err := rediscache.Init(&f.Config.Redis); err != nil {
		t.Fatal("initialize owned Redis")
	}
	result, ok := NewSystemService().Healthz(t.Context())
	if ok || result.DB != "ok" || result.Redis != "ok" || result.Storage != "ok" || result.DocEngine != "nok" {
		t.Fatal("actual dependency states", result)
	}
	dao.DB = nil
	rediscache.Close()
	storage.GetStorageFactory().SetStorage(nil)
	result, ok = NewSystemService().Healthz(t.Context())
	if ok || result.Status != "nok" || len(result.Meta) != 4 {
		t.Fatal(result)
	}
	for key, value := range result.Meta {
		if value.Elapsed == "" || value.Error != key+" health check failed" {
			t.Fatal("public failure reason", key, value)
		}
	}
}
func TestAfter7fbHealthzFailureReasons(t *testing.T) {
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	start := time.Now()
	result, ok := NewSystemService().Healthz(ctx)
	raw, err := json.Marshal(result)
	if ok || err != nil || time.Since(start) > time.Second || strings.Contains(string(raw), "password") || !strings.Contains(string(raw), `"status":"nok"`) {
		t.Fatal(string(raw), err)
	}
}
