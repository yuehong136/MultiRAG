package admin

import (
	"sync"
	"testing"
	"time"

	"multirag/internal/common"
)

func TestHeartbeatServiceStatusBoundary(t *testing.T) {
	now := time.Now()
	for _, tc := range []struct {
		name string
		last time.Time
		want string
	}{
		{"zero", time.Time{}, "timeout"},
		{"future", now.Add(time.Nanosecond), "timeout"},
		{"fresh", now, "alive"},
		{"before deadline", now.Add(-heartbeatTimeout + time.Nanosecond), "alive"},
		{"at deadline", now.Add(-heartbeatTimeout), "timeout"},
		{"after deadline", now.Add(-heartbeatTimeout - time.Nanosecond), "timeout"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := heartbeatServiceStatus(tc.last, now); got != tc.want {
				t.Fatalf("status=%s, want %s", got, tc.want)
			}
		})
	}
}

func TestHeartbeatReceiptTimeAndRecovery(t *testing.T) {
	previous := GlobalServerStatusStore
	store := &ServerStatusStore{servers: make(map[string]*common.BaseMessage)}
	GlobalServerStatusStore = store
	t.Cleanup(func() { GlobalServerStatusStore = previous })
	svc := &Service{}
	for _, timestamp := range []time.Time{{}, time.Now().Add(24 * time.Hour), time.Now().Add(-24 * time.Hour)} {
		before := time.Now()
		message := &common.BaseMessage{ServerName: "worker", ServerType: common.ServerTypeWorker, Host: "localhost", Port: 99, Timestamp: timestamp}
		if code, _ := svc.HandleHeartbeat(message); code != common.CodeLicenseValid {
			t.Fatal(code)
		}
		status, ok := store.GetStatus("worker")
		if !ok || status.Timestamp.Before(before) || status.Timestamp.After(time.Now()) || message.Timestamp != timestamp {
			t.Fatalf("receipt time or caller message changed: %+v, %+v", status, message)
		}
		// Readers and the original sender cannot change the stored snapshot.
		message.ServerName = "changed"
		status.Timestamp = time.Time{}
		store.GetAllStatuses()[0].ServerName = "changed"
		store.GetStatusesByType(common.ServerTypeWorker)[0].Host = "changed"
		assertHeartbeatList(t, svc, "alive")

		store.mu.Lock()
		store.servers["worker"].Timestamp = time.Now().Add(-heartbeatTimeout)
		store.mu.Unlock()
		assertHeartbeatList(t, svc, "timeout")
		// The timed-out entry stays visible and a new heartbeat revives it.
		svc.HandleHeartbeat(&common.BaseMessage{ServerName: "worker", ServerType: common.ServerTypeWorker, Host: "localhost", Port: 99})
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
			if item["status"] != want || item["host"] != "localhost" || item["port"] != 99 || item["service_type"] != common.ServerTypeWorker {
				t.Fatalf("unexpected heartbeat service: %v", item)
			}
			return
		}
	}
	t.Fatal("heartbeat service disappeared")
}

func TestHeartbeatConcurrentReportsAndReads(t *testing.T) {
	previous := GlobalServerStatusStore
	store := &ServerStatusStore{servers: make(map[string]*common.BaseMessage)}
	GlobalServerStatusStore = store
	t.Cleanup(func() { GlobalServerStatusStore = previous })
	svc := &Service{}
	var workers sync.WaitGroup
	for range 8 {
		workers.Go(func() {
			for range 100 {
				svc.HandleHeartbeat(&common.BaseMessage{ServerName: "worker", ServerType: common.ServerTypeWorker})
				store.GetStatus("worker")
				store.GetAllStatuses()
				store.GetStatusesByType(common.ServerTypeWorker)
				svc.ListServices()
			}
		})
	}
	workers.Wait()
	store.CleanupStaleStatuses(time.Hour)
	if _, ok := store.GetStatus("worker"); !ok {
		t.Fatal("fresh heartbeat was cleaned up")
	}
	store.RemoveStatus("worker")
	if _, ok := store.GetStatus("worker"); ok {
		t.Fatal("removed heartbeat remained")
	}
}
