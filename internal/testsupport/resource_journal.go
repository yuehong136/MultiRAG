// Package testsupport supplies fail-closed resource registration for controlled tests.
package testsupport

import (
	"encoding/json"
	"github.com/google/uuid"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

var journalMu sync.Mutex

type CaseJournal struct {
	t        testing.TB
	path, id string
}

func appendEvent(t testing.TB, path string, flags int, value any) {
	t.Helper()
	journalMu.Lock()
	defer journalMu.Unlock()
	f, err := os.OpenFile(path, flags|os.O_WRONLY|os.O_APPEND, 0600)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(value)
	if err == nil {
		_, err = f.Write(append(raw, '\n'))
	}
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		t.Fatal(err)
	}
	if closeErr != nil {
		t.Fatal(closeErr)
	}
}

// NewCase creates a separate durable case journal before fixtures or requests.
func NewCase(t testing.TB) *CaseJournal {
	t.Helper()
	id := uuid.NewString()
	root := os.Getenv("REST_DOCUMENT_RESOURCE_DIR")
	parent := os.Getenv("MULTIRAG_RUN_JOURNAL")
	if os.Getenv("MULTIRAG_RUN_ID") != "" && (root == "" || parent == "") {
		t.Fatal("registered runner and case directory required")
	}
	if root == "" {
		root = t.TempDir()
	}
	path := filepath.Join(root, id+".jsonl")
	if parent != "" {
		appendEvent(t, parent, 0, map[string]any{"event": "reserve-case-journal", "case_id": id, "path": path, "case": t.Name(), "parent_run_id": os.Getenv("MULTIRAG_RUN_ID")})
	}
	appendEvent(t, path, os.O_CREATE|os.O_EXCL, map[string]any{"event": "case", "case_id": id, "case": t.Name(), "parent_run_id": os.Getenv("MULTIRAG_RUN_ID"), "pid": os.Getpid()})
	dir, err := os.Open(root)
	if err != nil {
		t.Fatal(err)
	}
	err = dir.Sync()
	dir.Close()
	if err != nil {
		t.Fatal(err)
	}
	c := &CaseJournal{t: t, path: path, id: id}
	t.Cleanup(func() { c.Event("case-finished", map[string]any{"failed": t.Failed()}) })
	return c
}

func (c *CaseJournal) Event(event string, fields map[string]any) {
	c.t.Helper()
	value := map[string]any{"event": event, "case_id": c.id, "parent_run_id": os.Getenv("MULTIRAG_RUN_ID")}
	for k, v := range fields {
		value[k] = v
	}
	appendEvent(c.t, c.path, 0, value)
}
