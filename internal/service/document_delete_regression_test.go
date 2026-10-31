package service

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"multirag/internal/testsupport/scratch"
)

// Reuse the existing deletion contracts inside a freshly reserved database;
// their own helpers register exact Redis/object identities and independent
// cleanup. No historical shared database is needed for this regression run.
func TestDocumentDeleteRegressionScratch(t *testing.T) {
	f := scratch.New(t)
	path := filepath.Join(os.Getenv("TMPDIR"), f.Prefix+"-delete.json")
	f.Journal.Event("reserve-deletion-overlay", map[string]any{"path": path, "database": f.Database})
	raw, err := json.Marshal(f.Config)
	if err != nil {
		t.Fatal(err)
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = file.Write(raw); err != nil {
		file.Close()
		t.Fatal(err)
	}
	if err = file.Sync(); err != nil {
		file.Close()
		t.Fatal(err)
	}
	file.Close()
	t.Cleanup(func() {
		if err := os.Remove(path); err != nil {
			t.Error(err)
		}
		_, err := os.Stat(path)
		f.Journal.Event("cleanup-deletion-overlay", map[string]any{"path": path, "absent": os.IsNotExist(err)})
		if !os.IsNotExist(err) {
			t.Error("private deletion overlay survived")
		}
	})
	t.Setenv("MULTIRAG_AFTERFF_G_CONFIG", path)
	for _, test := range []struct {
		name string
		run  func(*testing.T)
	}{
		{"reject-before-cancel", TestDocumentDeleteLiveRejectsBeforeCancellation},
		{"cancel-compensation", TestDocumentDeleteLiveCancellationFailureCompensatesOwnNonce},
		{"store-failure", TestDocumentDeleteLiveUnknownObjectAndIndexFailuresRemainRetryable},
		{"sql-rollback", TestDocumentDeleteLiveFinalSQLFailureRollsBackAllCounters},
		{"shared-objects", TestDocumentDeleteLivePreservesSharedFileImageAndPendingWriter},
		{"missing-registration", TestDocumentDeleteLiveMissingFileAndRecoveryMirrorsBlockBeforeCancel},
		{"graph-lost-ack", TestDocumentDeleteLiveGraphLostAckInvalidatesBeforeRetry},
		{"all-pages", TestDocumentDeleteLiveAllSelectionReadsEveryPage},
		{"late-attach", TestDocumentDeleteLiveAttachWaitsForFileRetirement},
		{"earlier-attach", TestDocumentDeleteLiveEarlierAttachProtectsFileAndSource},
		{"source-recovery", TestDocumentDeleteLivePendingSQLRecoveryBlocksAndReadyRetires},
		{"native-drain", TestDocumentDeleteLiveCanceledHTTPDrainsNativeBeforeUnlock},
		{"shared-graph", TestDocumentDeleteLiveSharedGraphAndZeroCounterRows},
	} {
		t.Run(test.name, test.run)
	}
}
