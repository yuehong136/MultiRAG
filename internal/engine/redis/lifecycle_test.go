package redis

import (
	"multirag/internal/server"
	"multirag/internal/testsupport/scratch"
	"sync"
	"testing"
)

func TestCanonicalRedisLifecycleAndReadOnlyHealth(t *testing.T) {
	f := scratch.New(t)
	Close()
	t.Cleanup(func() { Close() })
	for _, cfg := range []*server.RedisConfig{nil, {Host: "127.0.0.1", Port: -1}, {Host: "127.0.0.1", Port: 65536}, {Host: "127.0.0.1", Port: 1, DB: -1}} {
		if err := Init(cfg); err == nil {
			t.Fatal("invalid Redis configuration accepted")
		}
	}
	bad := f.Config.Redis
	bad.Port = 1
	if err := Init(&bad); err == nil || Get() != nil || IsEnabled() {
		t.Fatal("failed init reported ready")
	}
	cfg := f.Config.Redis
	if err := Init(&cfg); err != nil {
		t.Fatal(err)
	}
	client := Get()
	cfg.Host = "changed.invalid"
	if client.config.Host != f.Config.Redis.Host {
		t.Fatal("caller mutated initialized config")
	}
	if !client.Health() {
		t.Fatal("actual Redis unavailable")
	}
	if count, err := f.Redis.DBSize(t.Context()).Result(); err != nil || count != 0 {
		t.Fatal("health wrote keys", count, err)
	}
	if err := Close(); err != nil || Get() != nil || IsEnabled() || client.Health() {
		t.Fatal("close retained readiness", err)
	}
	if err := Init(&f.Config.Redis); err != nil {
		t.Fatal("reinitialize after close", err)
	}
	var group sync.WaitGroup
	for range 8 {
		group.Go(func() {
			for range 10 {
				Init(&f.Config.Redis)
				if client := Get(); client != nil {
					client.Health()
				}
				Close()
				IsEnabled()
			}
		})
	}
	group.Wait()
}
