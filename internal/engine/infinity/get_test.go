package infinity

import "testing"

func TestGetChunkRequiresExplicitDatasetScope(t *testing.T) {
	e := &infinityEngine{}
	for _, base := range []string{"multirag_tenant", "multirag_doc_meta_tenant"} {
		if row, err := e.GetChunk(t.Context(), base, "c", nil); err != nil || row != nil {
			t.Fatal("empty scope reached uninitialized connection", row, err)
		}
		for _, scope := range [][]string{{""}, {" "}, {"kb", ""}, {"kb", "kb"}, {"kb-invalid"}} {
			_, err := e.GetChunk(t.Context(), base, "c", scope)
			if err == nil || err.Error() == "Infinity client not initialized" {
				t.Fatal("invalid scope was not rejected before borrowing connection", scope, err)
			}
		}
	}
}
