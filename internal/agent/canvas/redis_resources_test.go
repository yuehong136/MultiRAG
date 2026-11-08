package canvas

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
)

var redisJournalMu sync.Mutex

func redisResourceRecord(t *testing.T, value map[string]any) {
	t.Helper()
	path := os.Getenv("MULTIRAG_RUN_JOURNAL")
	if path == "" {
		if os.Getenv("MULTIRAG_RUN_ID") != "" {
			t.Fatal("missing registered journal")
		}
		return
	}
	redisJournalMu.Lock()
	defer redisJournalMu.Unlock()
	f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	value["case"] = t.Name()
	value["parent_run_id"] = os.Getenv("MULTIRAG_RUN_ID")
	if err := json.NewEncoder(f).Encode(value); err != nil {
		t.Fatal(err)
	}
	if err := f.Sync(); err != nil {
		t.Fatal(err)
	}
}

func registeredMiniRedis(t *testing.T) *miniredis.Miniredis {
	t.Helper()
	for attempt := 0; attempt < 10; attempt++ {
		token := make([]byte, 16)
		if _, err := rand.Read(token); err != nil {
			t.Fatal(err)
		}
		id := hex.EncodeToString(token)
		address := fmt.Sprintf("127.0.0.1:%d", 20000+(int(token[0])*256+int(token[1]))%30000)
		record := func(action string, extra map[string]any) {
			value := map[string]any{"action": action, "resource_id": id, "kind": "canvas-test-redis", "address": address}
			for k, v := range extra {
				value[k] = v
			}
			redisResourceRecord(t, value)
		}
		record("intent", nil)
		if conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond); err == nil {
			_ = conn.Close()
			record("occupied-borrowed", nil)
			continue
		}
		mr := miniredis.NewMiniRedis()
		if err := mr.StartAddr(address); err != nil {
			record("allocation-unavailable", nil)
			continue
		}
		record("allocated", nil)
		t.Cleanup(func() {
			mr.Close()
			conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
			absent := err != nil
			if conn != nil {
				_ = conn.Close()
			}
			record("cleanup-readback", map[string]any{"absent": absent})
			if !absent {
				t.Error("canvas Redis listener remains", address)
			}
		})
		return mr
	}
	t.Fatal("no unoccupied canvas Redis test address")
	return nil
}
