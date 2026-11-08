// Package canvas builds deterministic DAGs and bounded explicit Loop nodes.
package canvas

import (
	"context"
	"fmt"
	"sort"
	"strings"

	"multirag/internal/agent/runtime"
	"multirag/internal/agent/workflowx"

	"github.com/cloudwego/eino/compose"
)

func isLegacyNoOp(name string) bool {
	return legacyNoOpNames[strings.ToLower(name)]
}

func statePre(ctx context.Context, in map[string]any, state *CanvasState) (map[string]any, error) {
	if in == nil {
		in = map[string]any{}
	}
	if state != nil {
		if ctxState, _, _ := runtime.GetStateFromContext[*runtime.CanvasState](ctx); ctxState != nil && ctxState != state {
			if err := ctxState.RestoreCheckpoint(state); err != nil {
				return nil, fmt.Errorf("canvas: restore checkpoint state: %w", err)
			}
			if err := state.RestoreFrom(ctxState); err != nil {
				return nil, err
			}
		}
	}
	snapshot := map[string]map[string]any{}
	if state != nil {
		snapshot = state.Snapshot()
	}
	out := make(map[string]any, len(in)+1)
	for k, v := range in {
		out[k] = v
	}
	out["state"] = snapshot
	return out, nil
}

func statePost(ctx context.Context, out map[string]any, state *CanvasState) (map[string]any, error) {
	cpnID, _ := out["__cpn_id__"].(string)
	if cpnID == "" {
		return out, nil
	}
	ctxState, _, _ := runtime.GetStateFromContext[*runtime.CanvasState](ctx)
	for k, v := range out {
		if k == "__cpn_id__" || k == "state" || k == "__legacy_noop__" {
			continue
		}
		if state != nil {
			state.SetVar(cpnID, k, v)
		}
		if ctxState != nil {
			ctxState.SetVar(cpnID, k, v)
		}
	}
	if state != nil && ctxState != nil && state != ctxState {
		if err := state.RestoreFrom(ctxState); err != nil {
			return nil, err
		}
	}
	return out, nil
}

func BuildWorkflow(ctx context.Context, c *Canvas) (*compose.Workflow[map[string]any, map[string]any], error) {
	if c == nil {
		return nil, fmt.Errorf("canvas: nil canvas")
	}
	if len(c.Components) == 0 {
		return nil, fmt.Errorf("canvas: no components")
	}

	validated, err := validateCanvasAdjacency(c)
	if err != nil {
		return nil, err
	}
	// Every consumer below, including cycle and Loop expansion, uses the
	// same reciprocal graph. A missing side of an edge is not shorthand.
	c = validated
	for id, comp := range c.Components {
		// Their next-node selection is not consumed by this workflow yet.
		// Even a single child could execute when no branch was selected.
		if strings.EqualFold(comp.Obj.ComponentName, "Switch") || strings.EqualFold(comp.Obj.ComponentName, "Categorize") {
			return nil, fmt.Errorf("canvas: conditional routing at %q: %w", id, runtime.ErrNotImplemented)
		}
	}

	genState := func(ctx context.Context) *CanvasState {
		if state, _, _ := runtime.GetStateFromContext[*runtime.CanvasState](ctx); state != nil {
			return state
		}
		return NewCanvasState("", "")
	}

	wf := compose.NewWorkflow[map[string]any, map[string]any](
		compose.WithGenLocalState(genState),
	)

	if hasCycle(c) {
		return nil, fmt.Errorf("canvas: cyclic DSL is not supported; use an explicit Loop or UserFillUp node")
	}

	loopMembers := make(map[string]bool)
	loopNodes := make(map[string]*compose.WorkflowNode)
	for cpnID, comp := range c.Components {
		if !strings.EqualFold(comp.Obj.ComponentName, "Loop") {
			continue
		}
		exp, err := buildLoopExpansion(ctx, c, cpnID)
		if err != nil {
			return nil, err
		}
		var opts []workflowx.LoopOption
		if exp.MaxIters > 0 {
			opts = append(opts, workflowx.WithLoopMaxIterations(exp.MaxIters))
		}
		node, err := workflowx.AddLoopNode[map[string]any](
			ctx, wf, cpnID, exp.Sub, exp.ShouldQuit, opts...,
		)
		if err != nil {
			return nil, fmt.Errorf("canvas: install loop %q: %w", cpnID, err)
		}
		loopNodes[cpnID] = node
		for m := range exp.Members {
			loopMembers[m] = true
		}
	}

	type pendingEdge struct {
		cpn string
		up  string
	}
	pending := make([]pendingEdge, 0, 4*len(c.Components))
	nodes := make(map[string]*compose.WorkflowNode, len(c.Components))
	for cpnID := range c.Components {
		if _, isLoop := loopNodes[cpnID]; isLoop {
			for _, up := range c.Components[cpnID].Upstream {
				pending = append(pending, pendingEdge{cpn: cpnID, up: up})
			}
			continue
		}
		if loopMembers[cpnID] {
			continue
		}
		name := c.Components[cpnID].Obj.ComponentName
		if name == "" {
			return nil, fmt.Errorf("canvas: component %q has empty component_name", cpnID)
		}
		body, err := buildNodeBody(cpnID, name, c.Components[cpnID].Obj.Params)
		if err != nil {
			return nil, err
		}
		lambda := compose.InvokableLambda[map[string]any, map[string]any](body)
		node := wf.AddLambdaNode(cpnID, lambda,
			compose.WithStatePreHandler[map[string]any, *CanvasState](statePre),
			compose.WithStatePostHandler[map[string]any, *CanvasState](statePost),
			compose.WithNodeName(cpnID),
		)
		nodes[cpnID] = node
		for _, up := range c.Components[cpnID].Upstream {
			pending = append(pending, pendingEdge{cpn: cpnID, up: up})
		}
	}

	resolveNode := func(id string) *compose.WorkflowNode {
		if n, ok := nodes[id]; ok {
			return n
		}
		if n, ok := loopNodes[id]; ok {
			return n
		}
		return nil
	}
	first := make(map[string]bool, len(c.Components))
	for _, e := range pending {
		if e.cpn == e.up {
			return nil, fmt.Errorf("canvas: self-edge on %q", e.cpn)
		}
		if resolveNode(e.up) == nil {
			return nil, fmt.Errorf("canvas: component %q has unknown upstream %q", e.cpn, e.up)
		}
		cpnNode := resolveNode(e.cpn)
		if cpnNode == nil {
			return nil, fmt.Errorf("canvas: pending edge references unknown cpn %q", e.cpn)
		}
		if !first[e.cpn] {
			cpnNode.AddInput(e.up)
			first[e.cpn] = true
		} else {
			cpnNode.AddDependency(e.up)
		}
	}

	for cpnID, comp := range c.Components {
		if node, isLoop := loopNodes[cpnID]; isLoop {
			if len(comp.Upstream) == 0 && !first[cpnID] {
				node.AddInput(compose.START)
			}
			hasOuterDownstream := false
			for _, down := range comp.Downstream {
				if loopMembers[down] {
					continue
				}
				hasOuterDownstream = true
				break
			}
			if !hasOuterDownstream {
				wf.End().AddInput(cpnID, compose.ToField(cpnID))
			}
			continue
		}
		if loopMembers[cpnID] {
			continue
		}
		if len(comp.Upstream) == 0 {
			nodes[cpnID].AddInput(compose.START)
		}
		if len(comp.Downstream) == 0 {
			wf.End().AddInput(cpnID, compose.ToField(cpnID))
		}
	}

	return wf, nil
}

func snapshotOutputs(src map[string]map[string]any) map[string]map[string]any {
	out := make(map[string]map[string]any, len(src))
	for k, v := range src {
		cp := make(map[string]any, len(v))
		for kk, vv := range v {
			cp[kk] = vv
		}
		out[k] = cp
	}
	return out
}

// validateCanvasAdjacency rejects contradictory edge declarations before any
// component factory can allocate resources. Upstream order selects the primary
// data input; Downstream is derived from those validated edges so graph wiring,
// terminal detection, branch checks, and Loop membership share one topology.
func validateCanvasAdjacency(c *Canvas) (*Canvas, error) {
	ids := make([]string, 0, len(c.Components))
	for id := range c.Components {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	upstreamSets := make(map[string]map[string]bool, len(ids))
	downstreamSets := make(map[string]map[string]bool, len(ids))
	for _, id := range ids {
		comp := c.Components[id]
		// The registry trims names. Reject that implicit alias at the Canvas
		// boundary so guards, native dispatch and factories see the same name.
		if comp.Obj.ComponentName == "" || comp.Obj.ComponentName != strings.TrimSpace(comp.Obj.ComponentName) {
			return nil, fmt.Errorf("canvas: component %q has empty component_name or surrounding whitespace", id)
		}
		upstreamSets[id] = make(map[string]bool, len(comp.Upstream))
		downstreamSets[id] = make(map[string]bool, len(comp.Downstream))
		for _, up := range comp.Upstream {
			if up == id {
				return nil, fmt.Errorf("canvas: self-edge on %q", id)
			}
			if _, ok := c.Components[up]; !ok {
				return nil, fmt.Errorf("canvas: component %q has unknown upstream %q", id, up)
			}
			if upstreamSets[id][up] {
				return nil, fmt.Errorf("canvas: duplicate upstream edge %q -> %q", up, id)
			}
			upstreamSets[id][up] = true
		}
		for _, down := range comp.Downstream {
			if down == id {
				return nil, fmt.Errorf("canvas: self-edge on %q", id)
			}
			if _, ok := c.Components[down]; !ok {
				return nil, fmt.Errorf("canvas: component %q has unknown downstream %q", id, down)
			}
			if downstreamSets[id][down] {
				return nil, fmt.Errorf("canvas: duplicate downstream edge %q -> %q", id, down)
			}
			downstreamSets[id][down] = true
		}
	}
	for _, id := range ids {
		comp := c.Components[id]
		for _, up := range comp.Upstream {
			if !downstreamSets[up][id] {
				return nil, fmt.Errorf("canvas: edge %q -> %q is not reciprocal (missing downstream)", up, id)
			}
		}
		for _, down := range comp.Downstream {
			if !upstreamSets[down][id] {
				return nil, fmt.Errorf("canvas: edge %q -> %q is not reciprocal (missing upstream)", id, down)
			}
		}
	}
	validated := *c
	validated.Components = make(map[string]CanvasComponent, len(ids))
	for _, id := range ids {
		comp := c.Components[id]
		comp.Upstream = append([]string(nil), comp.Upstream...)
		comp.Downstream = nil
		validated.Components[id] = comp
	}
	for _, id := range ids {
		for _, up := range validated.Components[id].Upstream {
			parent := validated.Components[up]
			parent.Downstream = append(parent.Downstream, id)
			validated.Components[up] = parent
		}
	}
	return &validated, nil
}
