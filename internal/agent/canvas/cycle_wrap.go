package canvas

// hasCycle reports whether the canvas's Downstream / Upstream edges
// form at least one cycle (a self-edge, or a non-trivial strongly
// connected component).
//
// The check is a simple iterative Tarjan-style SCC walk — we do not
// need the full SCC decomposition, only a yes/no answer. The walk
// uses the explicit Downstream lists that the canvas already
// exposes; the loop's own internal edges (Begin↔Answer cycles
// inside an existing Loop sub-graph) are not relevant here because
// buildLoopExpansion has already consumed them by the time
// BuildWorkflow asks.
//
// Complexity: O(V + E) — single DFS over the components map, with
// early exit as soon as a back-edge is found. The fixture set has
// at most ~30 components per canvas, so a simple recursive
// implementation is more than fast enough.
func hasCycle(c *Canvas) bool {
	// Self-edge check — cheap, do it first.
	for cpnID, comp := range c.Components {
		for _, down := range comp.Downstream {
			if down == cpnID {
				return true
			}
		}
	}

	// Iterative DFS with three-colour marking: 0 = unvisited, 1 =
	// in current DFS stack, 2 = fully visited. A back-edge (an edge
	// to a node already in the current stack) means a cycle.
	const (
		unvisited = 0
		onStack   = 1
		done      = 2
	)
	state := make(map[string]int, len(c.Components))
	for start := range c.Components {
		if state[start] != unvisited {
			continue
		}
		// Stack entries: (cpn_id, index into Downstream).
		stack := []struct {
			cpn string
			i   int
		}{{cpn: start, i: 0}}
		state[start] = onStack
		for len(stack) > 0 {
			top := &stack[len(stack)-1]
			comp := c.Components[top.cpn]
			if top.i >= len(comp.Downstream) {
				state[top.cpn] = done
				stack = stack[:len(stack)-1]
				continue
			}
			down := comp.Downstream[top.i]
			top.i++
			if down == top.cpn {
				// Self-edge inside a Downstream list — already
				// filtered out by the early check, but kept here
				// as a defence-in-depth.
				return true
			}
			switch state[down] {
			case unvisited:
				state[down] = onStack
				stack = append(stack, struct {
					cpn string
					i   int
				}{cpn: down, i: 0})
			case onStack:
				return true
			case done:
				// Cross / forward edge into a fully-visited
				// component — cannot create a new cycle.
			}
		}
	}
	return false
}
