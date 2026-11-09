//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

// cycle_wrap.go contains cycle detection and the frozen synthetic-loop entry
// points. Generic cycles are unavailable: unrolling them once loses their
// conversational and control-flow semantics. Explicit Loop macros use the real
// builder in loop_subgraph.go.

package canvas

import (
	"context"
	"fmt"

	"multirag/internal/agent/runtime"
	"multirag/internal/agent/workflowx"

	"github.com/cloudwego/eino/compose"
)

// syntheticLoopKey is the cpn_id used for the synthetic Loop node
// that wraps a cyclic canvas. Using a reserved key avoids
// collisions with any user-defined cpn_id.
const syntheticLoopKey = "__synthetic_loop__"

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

// buildSyntheticLoop rejects synthetic one-pass execution of cyclic graphs.
func buildSyntheticLoop(ctx context.Context, c *Canvas) (*loopExpansion, error) {
	return nil, fmt.Errorf("canvas: synthetic cyclic execution: %w", runtime.ErrNotImplemented)
}

// alwaysQuitOption is a tiny helper: callers that need a one-iteration
// loop pass it as the LoopOption set so the workflowx cap matches
// shouldQuit's first-iteration behaviour.
func alwaysQuitOption() workflowx.LoopOption {
	return workflowx.WithLoopMaxIterations(1)
}

// compileSyntheticLoop rejects synthetic one-pass execution of cyclic graphs.
func compileSyntheticLoop(
	ctx context.Context,
	wf *compose.Workflow[map[string]any, map[string]any],
	exp *loopExpansion,
) (*compose.WorkflowNode, error) {
	return nil, fmt.Errorf("canvas: synthetic cyclic execution: %w", runtime.ErrNotImplemented)
}

// buildSubWorkflowBreakCycles rejects synthetic one-pass execution of cyclic graphs.
func buildSubWorkflowBreakCycles(
	ctx context.Context,
	c *Canvas,
	members map[string]bool,
	loopID string,
	initValues map[string]initVarSpec,
) (*compose.Workflow[map[string]any, map[string]any], error) {
	return nil, fmt.Errorf("canvas: synthetic cyclic execution: %w", runtime.ErrNotImplemented)
}
