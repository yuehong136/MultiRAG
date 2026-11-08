package canvas

import (
	"context"
	"errors"
	"strings"
	"testing"

	"multirag/internal/agent/runtime"
)

// These cases must be rejected before factories can resolve models, construct
// external clients, or allocate resources, regardless of map iteration order.
func TestBuildWorkflowRejectsAmbiguousAdjacencyBeforeFactories(t *testing.T) {
	cases := []struct {
		name        string
		graph       map[string]CanvasComponent
		message     string
		unavailable bool
	}{
		{
			name: "unknown downstream",
			graph: map[string]CanvasComponent{
				"a": {Obj: CanvasComponentObj{ComponentName: "SideEffect"}, Downstream: []string{"missing"}},
			},
			message: "unknown downstream",
		},
		{
			name: "unknown upstream",
			graph: map[string]CanvasComponent{
				"a": {Obj: CanvasComponentObj{ComponentName: "SideEffect"}, Upstream: []string{"missing"}},
			},
			message: "unknown upstream",
		},
		{
			name: "upstream only is not shorthand",
			graph: map[string]CanvasComponent{
				"a": {Obj: CanvasComponentObj{ComponentName: "SideEffect"}},
				"b": {Obj: CanvasComponentObj{ComponentName: "SideEffect"}, Upstream: []string{"a"}},
			},
			message: "not reciprocal",
		},
		{
			name: "downstream only is not shorthand",
			graph: map[string]CanvasComponent{
				"a": {Obj: CanvasComponentObj{ComponentName: "SideEffect"}, Downstream: []string{"b"}},
				"b": {Obj: CanvasComponentObj{ComponentName: "SideEffect"}},
			},
			message: "not reciprocal",
		},
		{
			name: "duplicate upstream",
			graph: map[string]CanvasComponent{
				"a": {Obj: CanvasComponentObj{ComponentName: "SideEffect"}, Downstream: []string{"b"}},
				"b": {Obj: CanvasComponentObj{ComponentName: "SideEffect"}, Upstream: []string{"a", "a"}},
			},
			message: "duplicate upstream",
		},
		{
			name: "duplicate downstream",
			graph: map[string]CanvasComponent{
				"a": {Obj: CanvasComponentObj{ComponentName: "SideEffect"}, Downstream: []string{"b", "b"}},
				"b": {Obj: CanvasComponentObj{ComponentName: "SideEffect"}, Upstream: []string{"a"}},
			},
			message: "duplicate downstream",
		},
	}
	for _, name := range []string{"Switch", "Categorize"} {
		cases = append(cases, struct {
			name        string
			graph       map[string]CanvasComponent
			message     string
			unavailable bool
		}{
			name: name + " inconsistent hidden second branch",
			graph: map[string]CanvasComponent{
				"branch": {Obj: CanvasComponentObj{ComponentName: name}, Downstream: []string{"left"}},
				"left":   {Obj: CanvasComponentObj{ComponentName: "SideEffect"}, Upstream: []string{"branch"}},
				"right":  {Obj: CanvasComponentObj{ComponentName: "SideEffect"}, Upstream: []string{"branch"}},
			}, message: "not reciprocal",
		})
		cases = append(cases, struct {
			name        string
			graph       map[string]CanvasComponent
			message     string
			unavailable bool
		}{
			name: name + " reciprocal branching unsupported",
			graph: map[string]CanvasComponent{
				"branch": {Obj: CanvasComponentObj{ComponentName: name}, Downstream: []string{"left", "right"}},
				"left":   {Obj: CanvasComponentObj{ComponentName: "SideEffect"}, Upstream: []string{"branch"}},
				"right":  {Obj: CanvasComponentObj{ComponentName: "SideEffect"}, Upstream: []string{"branch"}},
			}, message: "conditional routing", unavailable: true,
		})
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			original := runtime.DefaultFactory()
			calls := 0
			runtime.SetDefaultFactory(nil)
			runtime.SetDefaultFactory(func(string, map[string]any) (runtime.Component, error) { calls++; return adjacencyComponent{}, nil })
			t.Cleanup(func() { runtime.SetDefaultFactory(nil); runtime.SetDefaultFactory(original) })
			_, err := BuildWorkflow(context.Background(), &Canvas{Version: 1, Components: tc.graph})
			if err == nil || !strings.Contains(err.Error(), tc.message) {
				t.Fatalf("expected %q before factories, got %v", tc.message, err)
			}
			if tc.unavailable && !errors.Is(err, runtime.ErrNotImplemented) {
				t.Fatalf("conditional graph must remain unavailable: %v", err)
			}
			if calls != 0 {
				t.Fatalf("invalid adjacency invoked %d factories", calls)
			}
		})
	}
}

type adjacencyComponent struct{}

func (adjacencyComponent) Invoke(_ context.Context, input map[string]any) (map[string]any, error) {
	return input, nil
}

func TestValidatedAdjacencyPreservesInputOrderAndLoopTopology(t *testing.T) {
	graph := &Canvas{Components: map[string]CanvasComponent{
		"begin": {Obj: CanvasComponentObj{ComponentName: "Begin"}, Downstream: []string{"loop"}},
		"loop":  {Obj: CanvasComponentObj{ComponentName: "Loop"}, Upstream: []string{"begin"}, Downstream: []string{"a"}},
		"a":     {Obj: CanvasComponentObj{ComponentName: "Message"}, Upstream: []string{"loop"}, Downstream: []string{"z", "b"}},
		"z":     {Obj: CanvasComponentObj{ComponentName: "Message"}, Upstream: []string{"a"}, Downstream: []string{"tail"}},
		"b":     {Obj: CanvasComponentObj{ComponentName: "Message"}, Upstream: []string{"a"}, Downstream: []string{"tail"}},
		"tail":  {Obj: CanvasComponentObj{ComponentName: "Message"}, Upstream: []string{"z", "b"}},
	}}
	validated, err := validateCanvasAdjacency(graph)
	if err != nil {
		t.Fatal(err)
	}
	if got := validated.Components["tail"].Upstream; len(got) != 2 || got[0] != "z" || got[1] != "b" {
		t.Fatalf("primary input order changed: %v", got)
	}
	members := collectDescendants(validated, "loop")
	for _, id := range []string{"a", "b", "z", "tail"} {
		if !members[id] {
			t.Fatalf("Loop body lost %s", id)
		}
	}
	if len(members) != 4 {
		t.Fatalf("unexpected Loop membership: %v", members)
	}
	if hasCycle(validated) {
		t.Fatal("valid explicit Loop body became cyclic")
	}
	if got := graph.Components["a"].Downstream; len(got) != 2 || got[0] != "z" || got[1] != "b" {
		t.Fatalf("validation mutated caller graph: %v", got)
	}
	// A complete reciprocal Loop body still reaches its actual component factories.
	original := runtime.DefaultFactory()
	runtime.SetDefaultFactory(nil)
	runtime.SetDefaultFactory(func(string, map[string]any) (runtime.Component, error) { return adjacencyComponent{}, nil })
	t.Cleanup(func() { runtime.SetDefaultFactory(nil); runtime.SetDefaultFactory(original) })
	if _, err := BuildWorkflow(context.Background(), graph); err != nil {
		t.Fatalf("valid explicit Loop graph rejected: %v", err)
	}
}
