package canvas

import (
	"fmt"
	"sync"
	"testing"
)

// BenchmarkStateMutex reports state contention; no unmeasured threshold is a gate.
func BenchmarkStateMutex(b *testing.B) {
	state := NewCanvasState("benchmark", "benchmark")
	for i := 0; i < 100; i++ {
		state.SetVar(cpnID(i), "value", i)
	}
	b.ResetTimer()
	for n := 0; n < b.N; n++ {
		var wg sync.WaitGroup
		for g := 0; g < 100; g++ {
			wg.Add(1)
			go func(g int) {
				defer wg.Done()
				for i := 0; i < 100; i++ {
					_, _ = state.GetVar(cpnID(g) + "@value")
					state.SetVar(cpnID(g), "value", i)
				}
			}(g)
		}
		wg.Wait()
	}
}

func cpnID(i int) string { return fmt.Sprintf("cpn_%d", i) }
