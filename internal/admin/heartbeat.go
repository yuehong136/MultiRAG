package admin

import (
	"multirag/internal/common"
	"sync"
	"time"
)

// ServerStatusStore is a thread-safe global server status storage
type ServerStatusStore struct {
	mu      sync.RWMutex
	servers map[string]*common.BaseMessage // key: server_id
}

// GlobalServerStatusStore is the global instance
var GlobalServerStatusStore = &ServerStatusStore{
	servers: make(map[string]*common.BaseMessage),
}

// UpdateStatus updates or adds a server status
func (s *ServerStatusStore) UpdateStatus(serverName string, status *common.BaseMessage) {
	s.mu.Lock()
	defer s.mu.Unlock()
	// Use receipt time, including its monotonic clock, for liveness. A remote
	// clock can be missing, skewed or in the future. Own the stored snapshot so
	// the caller cannot change the server's identity or receipt time afterwards.
	snapshot := *status
	snapshot.Timestamp = time.Now()
	s.servers[serverName] = &snapshot
}

// GetStatus gets a single server status
func (s *ServerStatusStore) GetStatus(serverName string) (*common.BaseMessage, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	status, ok := s.servers[serverName]
	if !ok {
		return nil, false
	}
	snapshot := *status
	return &snapshot, true
}

// GetAllStatuses gets all server statuses
func (s *ServerStatusStore) GetAllStatuses() []*common.BaseMessage {
	s.mu.RLock()
	defer s.mu.RUnlock()
	result := make([]*common.BaseMessage, 0, len(s.servers))
	for _, status := range s.servers {
		snapshot := *status
		result = append(result, &snapshot)
	}
	return result
}

// GetStatusesByType gets server statuses by type
func (s *ServerStatusStore) GetStatusesByType(serverType common.ServerType) []*common.BaseMessage {
	s.mu.RLock()
	defer s.mu.RUnlock()
	result := make([]*common.BaseMessage, 0)
	for _, status := range s.servers {
		if status.ServerType == serverType {
			snapshot := *status
			result = append(result, &snapshot)
		}
	}
	return result
}

// RemoveStatus removes a server status
func (s *ServerStatusStore) RemoveStatus(serverID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.servers, serverID)
}

// CleanupStaleStatuses cleans up servers that haven't reported for a specified duration
func (s *ServerStatusStore) CleanupStaleStatuses(maxAge time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now()
	for id, status := range s.servers {
		if now.Sub(status.Timestamp) > maxAge {
			delete(s.servers, id)
		}
	}
}
