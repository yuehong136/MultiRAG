package admin

import (
	"sync"
	"testing"
	"time"

	"multirag/internal/common"
)

const heartbeatTimeout = 30 * time.Second

func TestHeartbeatServiceStatusBoundary(t *testing.T) {
	previous := GlobalServerStore
	store := &ServerStore{servers: map[string]*common.BaseMessage{}}
	GlobalServerStore = store
	t.Cleanup(func() { GlobalServerStore = previous })
	for _, tc := range []struct {
		name string
		last time.Time
		want string
	}{{"zero", time.Time{}, "timeout"}, {"future", time.Now().Add(time.Hour), "timeout"}, {"fresh", time.Now(), "alive"}, {"before deadline", time.Now().Add(-29 * time.Second), "alive"}, {"at deadline", time.Now().Add(-heartbeatTimeout), "timeout"}, {"after deadline", time.Now().Add(-31 * time.Second), "timeout"}} {
		t.Run(tc.name, func(t *testing.T) {
			store.UpdateServerInfo("worker", &common.BaseMessage{ServerName: "worker", ServerType: common.ServerTypeIngestion, Timestamp: tc.last, Host: "localhost", Port: 99})
			assertHeartbeatList(t, &Service{}, tc.want)
		})
	}
}

func TestHeartbeatReceiptTimeAndRecovery(t *testing.T) {
	previous := GlobalServerStore
	store := &ServerStore{servers: make(map[string]*common.BaseMessage)}
	GlobalServerStore = store
	t.Cleanup(func() { GlobalServerStore = previous })
	svc := &Service{}
	for _, timestamp := range []time.Time{{}, time.Now().Add(24 * time.Hour), time.Now().Add(-24 * time.Hour)} {
		before := time.Now()
		message := &common.BaseMessage{ServerName: "worker", ServerType: common.ServerTypeIngestion, Host: "localhost", Port: 99, Timestamp: timestamp, Ext: map[string]interface{}{"nested": map[string]interface{}{"value": "retained"}}}
		if code, _ := svc.HandleHeartbeat(message); code != common.CodeLicenseValid {
			t.Fatal(code)
		}
		status, ok := store.GetServerInfo("worker")
		if !ok || status.Timestamp.Before(before) || status.Timestamp.After(time.Now()) || message.Timestamp != timestamp {
			t.Fatalf("receipt time or caller message changed: %+v, %+v", status, message)
		}
		// Readers and the original sender cannot change the stored snapshot.
		message.ServerName = "changed"
		message.Ext.(map[string]interface{})["nested"].(map[string]interface{})["value"] = "changed"
		status.Ext.(map[string]interface{})["nested"].(map[string]interface{})["value"] = "changed"
		status.Timestamp = time.Time{}
		store.ListInfos()[0].ServerName = "changed"
		store.ListInfosByType(common.ServerTypeIngestion)[0].Host = "changed"
		assertHeartbeatList(t, svc, "alive")
		snapshot, _ := store.GetServerInfo("worker")
		if snapshot.Ext.(map[string]interface{})["nested"].(map[string]interface{})["value"] != "retained" {
			t.Fatal("nested heartbeat alias")
		}

		store.mu.Lock()
		store.servers["worker"].Timestamp = time.Now().Add(-heartbeatTimeout)
		store.mu.Unlock()
		assertHeartbeatList(t, svc, "timeout")
		// The timed-out entry stays visible and a new heartbeat revives it.
		svc.HandleHeartbeat(&common.BaseMessage{ServerName: "worker", ServerType: common.ServerTypeIngestion, Host: "localhost", Port: 99})
		assertHeartbeatList(t, svc, "alive")
	}
}

func assertHeartbeatList(t *testing.T, svc *Service, want string) {
	t.Helper()
	services, err := svc.ListServices()
	if err != nil {
		t.Fatal(err)
	}
	for _, item := range services {
		if item["name"] == "worker" {
			if item["status"] != want || item["host"] != "localhost" || item["port"] != 99 || item["service_type"] != common.ServerTypeIngestion {
				t.Fatalf("unexpected heartbeat service: %v", item)
			}
			return
		}
	}
	t.Fatal("heartbeat service disappeared")
}

func TestHeartbeatConcurrentReportsAndReads(t *testing.T) {
	previous := GlobalServerStore
	store := &ServerStore{servers: make(map[string]*common.BaseMessage)}
	GlobalServerStore = store
	t.Cleanup(func() { GlobalServerStore = previous })
	svc := &Service{}
	var workers sync.WaitGroup
	for range 8 {
		workers.Go(func() {
			for range 100 {
				svc.HandleHeartbeat(&common.BaseMessage{ServerName: "worker", ServerType: common.ServerTypeIngestion})
				store.GetServerInfo("worker")
				store.ListInfos()
				store.ListInfosByType(common.ServerTypeIngestion)
				svc.ListServices()
			}
		})
	}
	workers.Wait()
	store.CleanupStaleStatuses(time.Hour)
	if _, ok := store.GetServerInfo("worker"); !ok {
		t.Fatal("fresh heartbeat was cleaned up")
	}
	store.RemoveStatus("worker")
	if _, ok := store.GetServerInfo("worker"); ok {
		t.Fatal("removed heartbeat remained")
	}
}
