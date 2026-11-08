package service

import (
	"context"
	"errors"
	"testing"
)

type cleanupFaultStore struct {
	removed, checked []string
	first            error
}

func (s *cleanupFaultStore) GetContext(context.Context, string, string, ...string) ([]byte, error) {
	panic("unexpected read")
}
func (s *cleanupFaultStore) Put(string, string, []byte, ...string) error { panic("unexpected put") }
func (s *cleanupFaultStore) RemoveContext(_ context.Context, _ string, key string, _ ...string) error {
	s.removed = append(s.removed, key)
	if key == "working" {
		return s.first
	}
	return nil
}
func (s *cleanupFaultStore) ObjectExistsContext(_ context.Context, _ string, key string) (bool, error) {
	s.checked = append(s.checked, key)
	return key == "working", nil
}
func TestFileCommitCleanupAttemptsAllReservedObjects(t *testing.T) {
	failure := errors.New("first cleanup failure")
	st := &cleanupFaultStore{first: failure}
	err := cleanupSnapshotObjects(context.Background(), st, [][2]string{{"bucket", "snapshot"}, {"bucket", "working"}})
	if !errors.Is(err, failure) {
		t.Fatal("original error lost", err)
	}
	if len(st.removed) != 2 || st.removed[0] != "working" || st.removed[1] != "snapshot" || len(st.checked) != 2 {
		t.Fatalf("short cleanup: %+v %+v", st.removed, st.checked)
	}
}
