package canvas

import (
	"context"
	"errors"
	"reflect"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/cloudwego/eino/compose"
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

func TestCompile_NativeInterruptResumePreservesState(t *testing.T) {
	var calls atomic.Int32
	original := runtime.DefaultFactory()
	runtime.SetDefaultFactory(nil)
	runtime.SetDefaultFactory(func(name string, params map[string]any) (runtime.Component, error) {
		if name == "CheckpointSeed" {
			return componentFunc(func(ctx context.Context, input map[string]any) (map[string]any, error) {
				calls.Add(1)
				state, _, err := runtime.GetStateFromContext[*runtime.CanvasState](ctx)
				if err != nil {
					return nil, err
				}
				state.SetSys("query", "original query")
				state.SetGlobal("keep", "global")
				state.SetEnv("region", "local")
				return map[string]any{"value": "before wait"}, nil
			}), nil
		}
		return original(name, params)
	})
	t.Cleanup(func() { runtime.SetDefaultFactory(nil); runtime.SetDefaultFactory(original) })
	dsl := &Canvas{Components: map[string]CanvasComponent{
		"seed":    {Obj: CanvasComponentObj{ComponentName: "CheckpointSeed"}, Downstream: []string{"wait"}},
		"wait":    {Obj: CanvasComponentObj{ComponentName: "UserFillUp"}, Upstream: []string{"seed"}, Downstream: []string{"message"}},
		"message": {Obj: CanvasComponentObj{ComponentName: "Message", Params: map[string]any{"text": "{{seed@value}} / {{wait@user_input}} / {{sys.query}}"}}, Upstream: []string{"wait"}},
	}}
	store := &memoryCheckpoints{entries: map[string][]byte{}}
	cc, err := Compile(context.Background(), dsl, WithCheckPointStore(store))
	if err != nil {
		t.Fatal(err)
	}
	first := NewCanvasState("run", "task")
	_, err = cc.Workflow.Invoke(WithState(context.Background(), first), map[string]any{}, compose.WithCheckPointID("owned-test-checkpoint"))
	if !IsInterruptError(err) {
		t.Fatalf("first run = %v, want native interrupt", err)
	}
	contexts := ExtractInterruptContexts(err)
	id := FirstInterruptID(contexts)
	if id == "" {
		t.Fatalf("no native interrupt id: %v", err)
	}
	if calls.Load() != 1 {
		t.Fatalf("seed calls=%d", calls.Load())
	}
	if _, found, err := store.Get(context.Background(), "owned-test-checkpoint"); err != nil || !found {
		t.Fatalf("checkpoint not persisted: %v %v", found, err)
	}
	// A new process/request has a fresh context state and rebuilds the graph.
	cc, err = Compile(context.Background(), dsl, WithCheckPointStore(store))
	if err != nil {
		t.Fatal(err)
	}
	resumed := NewCanvasState("new", "new")
	ctx := compose.ResumeWithData(WithState(context.Background(), resumed), id, "followup")
	out, err := cc.Workflow.Invoke(ctx, nil, compose.WithCheckPointID("owned-test-checkpoint"))
	if err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 1 {
		t.Fatalf("upstream reran after resume: %d", calls.Load())
	}
	content, _ := resumed.GetVar("message@content")
	if content != "before wait / followup / original query" {
		t.Fatalf("resumed content=%v output=%v", content, out)
	}
	if resumed.GetSys("query") != "original query" || resumed.Env["region"] != "local" || resumed.Globals["keep"] != "global" {
		t.Fatalf("state lost on resume: %+v", resumed)
	}
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
