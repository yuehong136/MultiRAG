package component

import (
	"sort"
	"strings"
	"testing"
)

// Registration is checked by actual supported names and explicit rejection
// of unavailable nodes, rather than an aspirational count of implementations.
func TestVerifyRegistration_P1(t *testing.T) {
	names := RegisteredNames()
	have := make(map[string]bool, len(names))
	for _, n := range names {
		have[n] = true
	}

	// Always-present P0+P1 (12 names — ExitLoop removed).
	requiredP0P1 := []string{
		"agent", "begin", "categorize", "dataoperations",
		"invoke", "listoperations", "llm", "message", "stringtransform",
		"switch", "variableaggregator", "variableassigner",
	}
	var missing []string
	for _, e := range requiredP0P1 {
		if !have[e] {
			missing = append(missing, e)
		}
	}
	if len(missing) > 0 {
		t.Fatalf("missing P0/P1 components: %v (have %d: %v)", missing, len(names), names)
	}
	for _, name := range []string{"Answer", "Retrieval", "TavilySearch", "ExeSQL", "Iteration", "IterationItem"} {
		if _, err := New(name, nil); err == nil {
			t.Errorf("unsupported node %s must fail at construction", name)
		}
	}

	// ExitLoop must NOT be in the registry (legacy compat lives at
	// the canvas level, not here).
	if have["exitloop"] {
		t.Errorf("exitloop is registered; expected gone. Unsupported exit semantics must not appear available.")
	}
	if have["loopitem"] {
		t.Errorf("loopitem is registered; expected gone. The Python-era LoopItem node is collapsed into the workflowx.AddLoopNode wrapper.")
	}

	// Case-insensitive lookup on a real component to prove the
	// registry's name normalization works at lookup time.
	if c, err := New("Message", nil); err != nil || c == nil {
		t.Errorf("New(Message) failed: c=%v err=%v", c, err)
	}

	// Sorted output for stable error messages.
	sortedCopy := make([]string, len(names))
	copy(sortedCopy, names)
	sort.Strings(sortedCopy)
	if !equalStrings(sortedCopy, names) {
		t.Errorf("RegisteredNames() not sorted: got %v", names)
	}

	t.Logf("OK — %d components registered, sorted, case-insensitive lookup works: %s", len(names), strings.Join(names, ", "))
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
