package canvas

import (
	"context"
	"errors"
	"reflect"
	"sync"
	"testing"

	"multirag/internal/agent/runtime"
)

type memoryCheckpoints struct {
	mu      sync.Mutex
	entries map[string][]byte
}

func (s *memoryCheckpoints) Get(_ context.Context, id string) ([]byte, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	v, ok := s.entries[id]
	return append([]byte(nil), v...), ok, nil
}
func (s *memoryCheckpoints) Set(_ context.Context, id string, data []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.entries[id] = append([]byte(nil), data...)
	return nil
}
func (s *memoryCheckpoints) Delete(_ context.Context, id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.entries, id)
	return nil
}

type componentFunc func(context.Context, map[string]any) (map[string]any, error)

func (f componentFunc) Invoke(ctx context.Context, input map[string]any) (map[string]any, error) {
	return f(ctx, input)
}

func TestCompile_RejectsUnavailableGraphSemantics(t *testing.T) {
	tests := map[string]*Canvas{
		"cycle":  {Components: map[string]CanvasComponent{"a": {Obj: CanvasComponentObj{ComponentName: "Begin"}, Upstream: []string{"b"}, Downstream: []string{"b"}}, "b": {Obj: CanvasComponentObj{ComponentName: "Message"}, Upstream: []string{"a"}, Downstream: []string{"a"}}}},
		"branch": {Components: map[string]CanvasComponent{"a": {Obj: CanvasComponentObj{ComponentName: "Switch"}, Downstream: []string{"b", "c"}}, "b": {Obj: CanvasComponentObj{ComponentName: "Message"}, Upstream: []string{"a"}}, "c": {Obj: CanvasComponentObj{ComponentName: "Message"}, Upstream: []string{"a"}}}},
		"exit":   {Components: map[string]CanvasComponent{"a": {Obj: CanvasComponentObj{ComponentName: "ExitLoop"}}}},
	}
	for name, c := range tests {
		t.Run(name, func(t *testing.T) {
			if _, err := Compile(context.Background(), c); err == nil {
				t.Fatal("unimplemented semantics must not succeed")
			}
		})
	}
}

func TestCompile_RequiresFactory(t *testing.T) {
	original := runtime.DefaultFactory()
	runtime.SetDefaultFactory(nil)
	t.Cleanup(func() { runtime.SetDefaultFactory(original) })
	_, err := buildNodeBody("begin", "Begin", nil)
	if err == nil {
		t.Fatal("missing factory returned fake success")
	}
}

func TestCanvasState_IndependentNestedSnapshots(t *testing.T) {
	s := NewCanvasState("r", "t")
	source := map[string]any{"nested": []any{map[string]any{"value": "original"}}}
	s.SetVar("a", "value", source)
	source["nested"].([]any)[0].(map[string]any)["value"] = "caller mutation"
	got, _ := s.GetVar("a@value")
	got.(map[string]any)["nested"].([]any)[0].(map[string]any)["value"] = "reader mutation"
	snapshot := s.Snapshot()
	snapshot["a"]["value"].(map[string]any)["nested"].([]any)[0].(map[string]any)["value"] = "snapshot mutation"
	value, err := s.GetVar("a@value.nested.0.value")
	if err != nil || value != "original" {
		t.Fatalf("aliased state: %v %v", value, err)
	}
}

func TestLoop_ListNestedValueComparison(t *testing.T) {
	value := map[string]any{"items": []any{"one"}}
	if !listContains([]any{value}, map[string]any{"items": []any{"one"}}) {
		t.Fatal("nested value not found")
	}
	if !listEqual([]any{value}, []any{map[string]any{"items": []any{"one"}}}) {
		t.Fatal("nested lists not equal")
	}
	if reflect.DeepEqual(value, map[string]any{"items": []any{"two"}}) {
		t.Fatal("test values invalid")
	}
	if _, err := buildNodeBody("exit", "ExitLoop", nil); !errors.Is(err, runtime.ErrNotImplemented) {
		t.Fatalf("exit error=%v", err)
	}
}
