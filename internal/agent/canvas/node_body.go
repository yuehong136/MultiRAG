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

// node_body.go — per-node lambda body construction.
//
// Both the outer graph (scheduler.go) and the Loop sub-graph
// (loop_subgraph.go) install lambda nodes that:
//
//  1. tag their output with __cpn_id__ so statePost can persist the
//     result into Outputs[cpnID]["result"];
//  2. either invoke a real factory-built component or fall back to a
//     no-op echo body.
//
// Centralising the construction here keeps both call sites consistent
// and makes the legacy-no-op / factory / placeholder routing logic the
// single source of truth.
package canvas

import (
	"context"
	"fmt"

	"multirag/internal/agent/runtime"
)

// nodeBodyFn is the plain function shape compose.InvokableLambda accepts.
// We avoid a named type alias because compose.InvokableLambda's generic
// inference only accepts the underlying func literal type, not a named
// alias on top of it.
type nodeBodyFn = func(ctx context.Context, in map[string]any) (map[string]any, error)

// buildNodeBody constructs a real component body. Unknown components,
// unavailable sentinels and a missing component factory are explicit errors.
func buildNodeBody(cpnID, name string, params map[string]any) (nodeBodyFn, error) {
	if isLegacyNoOp(name) {
		return nil, fmt.Errorf("canvas: component %q (%s): %w", cpnID, name, runtime.ErrNotImplemented)
	}
	if factory := runtime.DefaultFactory(); factory != nil {
		comp, err := factory(name, params)
		if err != nil {
			return nil, fmt.Errorf("canvas: component %q (%s): factory: %w", cpnID, name, err)
		}
		if comp == nil {
			return nil, fmt.Errorf("canvas: component %q (%s): factory returned nil component", cpnID, name)
		}
		return realComponentBody(cpnID, comp), nil
	}
	// Fallback: no factory registered. This path is only exercised by
	// canvas-only unit tests; production wiring always installs a
	// factory via component.init().
	if !isKnownPrimitive(name) {
		return nil, fmt.Errorf("canvas: component %q has unknown component_name %q (typo? not in the Phase 1 primitive allowlist, not in legacyNoOpNames)", cpnID, name)
	}
	return nil, fmt.Errorf("canvas: component %q (%s): component factory not configured: %w", cpnID, name, runtime.ErrNotImplemented)
}

// legacyNoOpBody rejects an unavailable DSL sentinel without echoing success.
func legacyNoOpBody(cpnID string) nodeBodyFn {
	return func(context.Context, map[string]any) (map[string]any, error) {
		return nil, fmt.Errorf("canvas: legacy component %q: %w", cpnID, runtime.ErrNotImplemented)
	}
}

// realComponentBody returns a body that delegates to the supplied
// runtime.Component. The component is constructed once at build time
// (in buildNodeBody) and re-invoked per iteration.
//
// The output map is tagged with __cpn_id__ before return so statePost
// can attribute the result; if the component already populated that
// key it is overwritten with the canvas-controlled value to keep
// attribution authoritative.
func realComponentBody(cpnID string, comp runtime.Component) nodeBodyFn {
	return func(ctx context.Context, in map[string]any) (map[string]any, error) {
		out, err := comp.Invoke(ctx, in)
		if err != nil {
			return nil, fmt.Errorf("canvas: component %q invoke: %w", cpnID, err)
		}
		if out == nil {
			out = make(map[string]any, 1)
		}
		out["__cpn_id__"] = cpnID
		return out, nil
	}
}

// placeholderBody reports the missing component factory.
func placeholderBody(cpnID string) nodeBodyFn {
	return func(ctx context.Context, in map[string]any) (map[string]any, error) {
		out, err := placeholderLambda(ctx, in)
		if err != nil {
			return nil, err
		}
		out["__cpn_id__"] = cpnID
		return out, nil
	}
}

// withStateBracket wraps body so that it performs the same pre/post
// state work as the outer-graph's eino StatePreHandler / StatePostHandler
// pair, but reads the state from the request context (attached via
// runtime.WithState) instead of an eino-managed graph-local state.
//
// This is the path used by the Loop sub-graph: its nodes do not have
// access to the outer graph's WithGenLocalState, but they do inherit
// the context-attached *CanvasState that the outer graph (or the
// invoking caller) installed. Wrapping the body lets sub-graph nodes
// participate in the same state snapshot / result-persistence
// contract as outer nodes.
//
// If no state is attached to ctx (e.g. a sub-graph test that runs
// the body directly), the wrapper degrades to a plain invocation:
// the body still runs, its output is still tagged with __cpn_id__,
// but no state snapshot is injected and no result is persisted.
func withStateBracket(body nodeBodyFn) nodeBodyFn {
	return func(ctx context.Context, in map[string]any) (map[string]any, error) {
		state, _, _ := runtime.GetStateFromContext[*runtime.CanvasState](ctx)
		if state != nil {
			if in == nil {
				in = map[string]any{}
			}
			snapshot := state.Snapshot()
			wrapped := make(map[string]any, len(in)+1)
			for k, v := range in {
				wrapped[k] = v
			}
			wrapped["state"] = snapshot
			in = wrapped
		}
		out, err := body(ctx, in)
		if err != nil {
			return nil, err
		}
		if state == nil || out == nil {
			return out, nil
		}
		cpnID, _ := out["__cpn_id__"].(string)
		if cpnID == "" {
			return out, nil
		}
		for k, v := range out {
			if k == "__cpn_id__" || k == "state" || k == "__legacy_noop__" {
				continue
			}
			state.SetVar(cpnID, k, v)
		}
		return out, nil
	}
}
