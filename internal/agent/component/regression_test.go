package component

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"sync"
	"time"

	"reflect"
	"strings"
	"testing"

	"multirag/internal/agent/canvas"
)

func TestExactNumericConfiguration(t *testing.T) {
	modelID := json.Number("9007199254740993")
	if got, ok := stringFrom(map[string]any{"model_id": modelID}, "model_id"); !ok || got != "9007199254740993" {
		t.Fatal(got, ok)
	}
	for _, invalid := range []any{1.1, math.NaN(), math.Inf(1), json.Number("1.1")} {
		if _, ok := intFrom(map[string]any{"value": invalid}, "value"); ok {
			t.Fatalf("accepted integer %v", invalid)
		}
	}
	if _, ok := floatFrom(map[string]any{"value": json.Number("1e1000")}, "value"); ok {
		t.Fatal("accepted nonfinite temperature")
	}
}
func TestBegin_RejectsIdentityOverride(t *testing.T) {
	node, _ := NewBeginComponent(nil)
	state := canvas.NewCanvasState("run", "task")
	state.Sys["user_id"] = "actor-a"
	ctx := canvas.WithState(context.Background(), state)
	if _, err := node.Invoke(ctx, map[string]any{"query": "hello", "user_id": "actor-b"}); err == nil {
		t.Fatal("identity override accepted")
	}
	if state.Sys["user_id"] != "actor-a" {
		t.Fatal("identity changed")
	}
}
func TestListOperations_NegativeAndStrictBounds(t *testing.T) {
	state := canvas.NewCanvasState("run", "task")
	state.SetVar("source", "items", []any{"first", "last"})
	ctx := canvas.WithState(context.Background(), state)
	node, _ := NewListOperationsComponent(map[string]any{"query": "source@items", "operations": "nth", "n": -1})
	out, err := node.Invoke(ctx, nil)
	if err != nil || !reflect.DeepEqual(out["result"], []any{"last"}) {
		t.Fatalf("negative nth %v,%v", out, err)
	}
	for _, op := range []string{"nth", "head", "tail"} {
		node, _ := NewListOperationsComponent(map[string]any{"query": "source@items", "operations": op, "n": 3, "strict": true})
		if out, err := node.Invoke(ctx, nil); err == nil || out != nil {
			t.Fatalf("strict %s accepted %v,%v", op, out, err)
		}
	}
}
func TestDataOperations_CombineFlattensAndPreservesSource(t *testing.T) {
	source := []any{1, 2}
	node := &DataOperationsComponent{}
	out := node.opCombine([]map[string]any{{"x": 0}, {"x": source}})
	if !reflect.DeepEqual(out["x"], []any{0, 1, 2}) {
		t.Fatal(out)
	}
	node.opCombine([]map[string]any{{"x": source}, {"x": 3}})
	if !reflect.DeepEqual(source, []any{1, 2}) {
		t.Fatal("source mutated", source)
	}
}
func TestVariableAggregator_TypedSelectors(t *testing.T) {
	state := canvas.NewCanvasState("run", "task")
	state.Sys["answer"] = "value"
	ctx := canvas.WithState(context.Background(), state)
	node, err := NewVariableAggregatorComponent(map[string]any{"groups": []map[string]any{{"group_name": "result", "variables": []map[string]any{{"value": "sys.answer"}}}}})
	if err != nil {
		t.Fatal(err)
	}
	out, err := node.Invoke(ctx, nil)
	if err != nil || out["result"] != "value" {
		t.Fatalf("typed selector %v,%v", out, err)
	}
}
func TestStringTransform_MergeWhitespace(t *testing.T) {
	node, err := NewStringTransformComponent(map[string]any{"method": "merge", "script": "Hello {{ name }}"})
	if err != nil {
		t.Fatal(err)
	}
	ctx := canvas.WithState(context.Background(), canvas.NewCanvasState("run", "task"))
	out, err := node.Invoke(ctx, map[string]any{"name": "Alice"})
	if err != nil || out["result"] != "Hello Alice" {
		t.Fatal(out, err)
	}
}
func TestDocsGenerator_RealBytesNoFakeDownload(t *testing.T) {
	node, err := NewDocsGenerator(map[string]any{"output_format": "txt", "content": "hello", "add_timestamp": false})
	if err != nil {
		t.Fatal(err)
	}
	out, err := node.Invoke(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(out["bytes"].([]byte)), "hello") {
		t.Fatal(out)
	}
	if _, present := out["download"]; present {
		t.Fatal("unserved fake download advertised")
	}
}

func TestMessage_RejectsSavedMemoryPersistence(t *testing.T) {
	if node, err := NewMessageComponent(map[string]any{"text": "message", "memory_save": true}); node != nil || !errors.Is(err, ErrNotImplemented) {
		t.Fatalf("static memory request ignored: %v,%v", node, err)
	}
	node, err := NewMessageComponent(map[string]any{"text": "message", "stream": true})
	if err != nil {
		t.Fatal(err)
	}
	ctx := canvas.WithState(context.Background(), canvas.NewCanvasState("run", "task"))
	out, err := node.Invoke(ctx, map[string]any{"stream": true})
	if err != nil || out["streamed_chunks"] != 1 {
		t.Fatal(out, err)
	}
}

func TestDocsGenerator_RejectsUnsupportedFormatOptions(t *testing.T) {
	for _, params := range []map[string]any{
		{"output_format": "txt", "watermark_text": "DRAFT"},
		{"output_format": "markdown", "watermark_text": "DRAFT"},
		{"output_format": "txt", "add_page_numbers": true},
		{"output_format": "html", "add_page_numbers": true},
	} {
		if node, err := NewDocsGenerator(params); node != nil || !errors.Is(err, ErrNotImplemented) {
			t.Fatalf("unsupported option ignored: %v => %v,%v", params, node, err)
		}
	}
	node, err := NewDocsGenerator(map[string]any{"output_format": "txt", "content": "body", "header_text": "header", "footer_text": "footer", "add_timestamp": false})
	if err != nil {
		t.Fatal(err)
	}
	out, err := node.Invoke(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if got := string(out["bytes"].([]byte)); got != "header\n\nbody\nfooter" {
		t.Fatal(got)
	}

}

func TestLLM_ProviderConfigurationIsolation(t *testing.T) {
	previous := http.DefaultTransport
	http.DefaultTransport = componentRoundTripper(func(request *http.Request) (*http.Response, error) {
		var payload map[string]any
		if err := json.NewDecoder(request.Body).Decode(&payload); err != nil {
			return nil, err
		}
		model := payload["model"].(string)
		key := strings.TrimPrefix(request.Header.Get("Authorization"), "Bearer ")
		if key != "key-"+model {
			return nil, fmt.Errorf("provider configuration crossed requests")
		}
		data, _ := json.Marshal(map[string]any{"id": "controlled", "model": model, "choices": []any{map[string]any{"index": 0, "finish_reason": "stop", "message": map[string]any{"role": "assistant", "content": model}}}})
		return response(200, string(data)), nil
	})
	t.Cleanup(func() { http.DefaultTransport = previous })
	withStubInvoker(t, &einoChatInvoker{})
	var group sync.WaitGroup
	for _, name := range []string{"tenant-a", "tenant-b"} {
		group.Add(1)
		go func(name string) {
			defer group.Done()
			node := NewLLMComponent(LLMParam{Driver: "openai", ModelID: name, APIKey: "key-" + name, BaseURL: "https://8.8.8.8/v1", UserPrompt: "hello"})
			out, err := node.Invoke(context.Background(), nil)
			if err != nil || out["content"] != name {
				t.Errorf("provider request isolation: %v %v", out, err)
			}
		}(name)
	}
	group.Wait()
}

func TestLLM_PropagatesActiveProviderCancellation(t *testing.T) {
	previous := http.DefaultTransport
	started := make(chan struct{}, 1)
	http.DefaultTransport = componentRoundTripper(func(request *http.Request) (*http.Response, error) {
		started <- struct{}{}
		<-request.Context().Done()
		return nil, request.Context().Err()
	})
	t.Cleanup(func() { http.DefaultTransport = previous })
	withStubInvoker(t, &einoChatInvoker{})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	result := make(chan error, 1)
	node := NewLLMComponent(LLMParam{Driver: "openai", ModelID: "fixture", APIKey: "fixture", BaseURL: "https://8.8.8.8/v1", UserPrompt: "hello"})
	go func() { _, err := node.Invoke(ctx, nil); result <- err }()
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("provider request did not start")
	}
	cancel()
	select {
	case err := <-result:
		if !errors.Is(err, context.Canceled) {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("provider ignored caller cancellation")
	}
}
