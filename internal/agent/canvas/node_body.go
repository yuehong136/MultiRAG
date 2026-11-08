// Node bodies use a registered component factory; no placeholder execution path exists.

package canvas

import (
	"context"
	"fmt"
	"strings"

	"multirag/internal/agent/runtime"
)

type nodeBodyFn = func(ctx context.Context, in map[string]any) (map[string]any, error)

func buildNodeBody(cpnID, name string, params map[string]any) (nodeBodyFn, error) {
	if isLegacyNoOp(name) {
		return nil, fmt.Errorf("canvas: component %q (%s): %w", cpnID, name, runtime.ErrNotImplemented)
	}
	if strings.EqualFold(name, "UserFillUp") {
		if err := validateUserFillUpInputs(params); err != nil {
			return nil, fmt.Errorf("canvas: component %q: %w", cpnID, err)
		}
		return UserFillUpNodeBody(cpnID, params), nil
	}
	factory := runtime.DefaultFactory()
	if factory == nil {
		return nil, fmt.Errorf("canvas: no component factory registered for %q (%s)", cpnID, name)
	}
	comp, err := factory(name, params)
	if err != nil {
		return nil, fmt.Errorf("canvas: component %q (%s): factory: %w", cpnID, name, err)
	}
	if comp == nil {
		return nil, fmt.Errorf("canvas: component %q (%s): factory returned nil component", cpnID, name)
	}
	return realComponentBody(cpnID, comp), nil
}

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
