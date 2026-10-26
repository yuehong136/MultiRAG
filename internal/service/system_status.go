package service

import (
	"context"
	"encoding/json"
	"multirag/internal/cache"
	"multirag/internal/server"
	"time"
)

type ComponentStatus struct {
	Type    string `json:"type,omitempty"`
	Status  string `json:"status"`
	Elapsed string `json:"elapsed"`
	Reason  string `json:"reason,omitempty"`
	Error   string `json:"error,omitempty"`
}
type StatusResponse struct {
	DocEngine              ComponentStatus             `json:"doc_engine"`
	Storage                ComponentStatus             `json:"storage"`
	Database               ComponentStatus             `json:"database"`
	Redis                  ComponentStatus             `json:"redis"`
	TaskExecutorHeartbeats map[string][]map[string]any `json:"task_executor_heartbeats"`
	HeartbeatError         string                      `json:"task_executor_heartbeat_error,omitempty"`
}

// GetStatus shares real bounded probes with readiness, then adds worker telemetry.
func (s *SystemService) GetStatus(ctx context.Context) *StatusResponse {
	health, _ := s.Healthz(ctx)
	component := func(key, state, typ string) ComponentStatus {
		m := health.Meta[key]
		status := "red"
		if state == "ok" {
			status = "green"
		}
		return ComponentStatus{Type: typ, Status: status, Elapsed: m.Elapsed, Reason: m.Reason, Error: m.Error}
	}
	var doc, store, db string
	if cfg := server.GetConfig(); cfg != nil {
		doc = string(cfg.DocEngine.Type)
		store = string(cfg.StorageEngine.Type)
		db = cfg.Database.Driver
	}
	result := &StatusResponse{DocEngine: component("doc_engine", health.DocEngine, doc), Storage: component("storage", health.Storage, store), Database: component("db", health.DB, db), Redis: component("redis", health.Redis, "redis"), TaskExecutorHeartbeats: map[string][]map[string]any{}}
	if cache.Get() == nil {
		result.HeartbeatError = "Worker telemetry unavailable."
		return result
	}
	bounded, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	rows, err := cache.Get().TaskExecutorHeartbeats(bounded)
	if err != nil {
		result.HeartbeatError = "Worker telemetry unavailable."
		return result
	}
	for id, values := range rows {
		result.TaskExecutorHeartbeats[id] = []map[string]any{}
		for _, raw := range values {
			var value map[string]any
			if json.Unmarshal([]byte(raw), &value) != nil || value == nil {
				result.HeartbeatError = "Malformed worker telemetry."
				continue
			}
			safe := map[string]any{}
			for _, key := range []string{"now", "boot_at", "pending", "lag", "done", "failed"} {
				if v, ok := value[key]; ok {
					safe[key] = v
				}
			}
			result.TaskExecutorHeartbeats[id] = append(result.TaskExecutorHeartbeats[id], safe)
		}
	}
	return result
}
