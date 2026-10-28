package service

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"

	"multirag/internal/entity"
)

func TestDocumentDeleteSelectionNeverBroadensAnEmptyList(t *testing.T) {
	for _, test := range []struct {
		ids []string
		all bool
	}{
		{nil, false}, {[]string{}, true}, {[]string{"a"}, true},
		{[]string{""}, false}, {[]string{" a"}, false}, {[]string{"a\n"}, false},
	} {
		if _, err := normalizedDocumentDeleteIDs(test.ids, test.all); !errors.Is(err, ErrInvalidDocumentDeleteRequest) {
			t.Fatalf("selection accepted: %#v", test)
		}
	}
	if ids, err := normalizedDocumentDeleteIDs([]string{}, false); err != nil || ids == nil || len(ids) != 0 {
		t.Fatal("explicit [] changed meaning", ids, err)
	}
	if ids, err := normalizedDocumentDeleteIDs(nil, true); err != nil || ids != nil {
		t.Fatal("delete_all rejected", err)
	}
	if ids, err := normalizedDocumentDeleteIDs([]string{"z", "a", "z", "b"}, false); err != nil || !reflect.DeepEqual(ids, []string{"a", "b", "z"}) {
		t.Fatal(ids, err)
	}
	if _, err := NewDocumentDeleteService(nil, nil, nil, nil).DeleteDocuments(context.Background(), "dataset", "user", []string{}, false); !errors.Is(err, ErrDocumentDeleteRetryable) {
		t.Fatal("empty selection skipped authorization", err)
	}
}

func TestDocumentDeleteRecoveryWireRejectsAmbiguousAuthority(t *testing.T) {
	base := `{"payload":{"version":1,"document_id":"doc","task_id":"task","nonce":"nonce","phase":"ready"},"types":[]}`
	for _, wire := range []string{
		strings.Replace(base, `"types":[]`, `"types":null`, 1),
		strings.Replace(base, `"types":[]`, `"types":{}`, 1),
		strings.Replace(base, `"version":1`, `"version":1,"version":1`, 1),
		strings.Replace(base, `"payload":`, `"payload":{},"payload":`, 1),
		strings.Replace(base, `"types":[]`, `"types":[{"path":["missing"],"type":"bytes"}]`, 1),
		strings.Replace(base, `"types":[]`, `"types":[{"path":null,"type":"date"}]`, 1),
		strings.Replace(base, `"phase":"ready"`, `"phase":"unknown"`, 1),
		strings.Replace(base, `"version":1`, `"version":1.0`, 1),
	} {
		if err := validateDocumentSourceRecovery(documentDeleteRecovery{ID: "nonce", DocumentID: "doc", TaskID: "task", Wire: wire}); err == nil {
			t.Fatal("ambiguous recovery authority accepted", wire)
		}
	}
	if err := validateDocumentSourceRecovery(documentDeleteRecovery{ID: "nonce", DocumentID: "doc", TaskID: "task", Wire: base}); err != nil {
		t.Fatal(err)
	}
	valid := `{"payload":{"version":1,"document_id":"doc","task_id":"task","nonce":"nonce","phase":"ready","at":"2026-10-28T18:00:00.123456","day":"2026-10-28","raw":"","amount":"0.0"},"types":[{"path":["at"],"type":"datetime"},{"path":["day"],"type":"date"},{"path":["raw"],"type":"bytes"},{"path":["amount"],"type":"decimal"}]}`
	if err := validateDocumentSourceRecovery(documentDeleteRecovery{ID: "nonce", DocumentID: "doc", TaskID: "task", Wire: valid}); err != nil {
		t.Fatal("Python wire type tags rejected", err)
	}
}

func TestDocumentDeleteSnapshotRejectsReplacementProducer(t *testing.T) {
	canceled, running, message := "2", "1", taskCancelMarker
	before := []entity.Document{{ID: "document", KbID: "dataset", ChunkNum: 2, TokenNum: 7}}
	after := []entity.Document{{ID: "document", KbID: "dataset", ChunkNum: 2, TokenNum: 7, Run: &canceled}}
	if !sameDeleteDocuments(before, after) {
		t.Fatal("cancellation changed identity")
	}
	after[0].Run = &running
	if sameDeleteDocuments(before, after) {
		t.Fatal("replacement work accepted")
	}
	after[0].Run = &canceled
	after[0].TokenNum++
	if sameDeleteDocuments(before, after) {
		t.Fatal("changed ledger accepted")
	}
	old := []entity.Task{{ID: "old-task", DocID: "document"}}
	current := []entity.Task{{ID: "old-task", DocID: "document", Progress: -1, ProgressMsg: &message}}
	if !sameDeleteTasks(old, current) {
		t.Fatal("canceled producer rejected")
	}
	current[0].ID = "replacement"
	if sameDeleteTasks(old, current) {
		t.Fatal("replacement token accepted")
	}
}

func TestDocumentDeleteFailureKeepsSecretsOutOfResponse(t *testing.T) {
	secret := errors.New("provider response includes secret-placeholder")
	err := documentDeleteError("object deletion failed", secret)
	if err.Error() != ErrDocumentDeleteRetryable.Error()+": object deletion failed" || !errors.Is(err, secret) || !errors.Is(err, ErrDocumentDeleteRetryable) {
		t.Fatal("error lost cause or exposed raw provider body", err)
	}
}
