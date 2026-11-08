package component

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"net/http"

	"github.com/cloudwego/eino/schema"
	"reflect"
	"strings"
	"sync"
	"testing"

	"multirag/internal/agent/canvas"
)

func TestLLM_ContextInvokerIsolation(t *testing.T) {
	var group sync.WaitGroup
	for _, answer := range []string{"tenant-a", "tenant-b"} {
		group.Add(1)
		go func(answer string) {
			defer group.Done()
			invoker := &stubInvoker{resp: &ChatInvokeResponse{Content: answer}}
			ctx := WithChatInvoker(context.Background(), invoker)
			node := NewLLMComponent(LLMParam{ModelID: "42", UserPrompt: "hello"})
			out, err := node.Invoke(ctx, nil)
			if err != nil || out["content"] != answer {
				t.Errorf("cross-context result %v,%v", out, err)
			}
		}(answer)
	}
	group.Wait()
}
func TestLLM_NoCredentialsOrDummyFallback(t *testing.T) {
	node := NewLLMComponent(LLMParam{ModelID: "42", UserPrompt: "hello"})
	if out, err := node.Invoke(context.Background(), nil); out != nil || !errors.Is(err, ErrNotImplemented) {
		t.Fatalf("unbound production model succeeded: %v,%v", out, err)
	}
	invoker := &stubInvoker{resp: &ChatInvokeResponse{Content: "must not be called"}}
	ctx := WithChatInvoker(context.Background(), invoker)
	for _, key := range []string{"driver", "api_key", "base_url"} {
		if _, err := node.Invoke(ctx, map[string]any{key: "untrusted"}); err == nil {
			t.Fatalf("accepted DSL %s", key)
		}
	}
	if invoker.calls != 0 {
		t.Fatal("provider called before credential rejection")
	}
}
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
	state.SetSys("user_id", "actor-a")
	ctx := canvas.WithState(context.Background(), state)
	if _, err := node.Invoke(ctx, map[string]any{"query": "hello", "user_id": "actor-b"}); err == nil {
		t.Fatal("identity override accepted")
	}
	if state.GetSys("user_id") != "actor-a" {
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
	state.SetSys("answer", "value")
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

func TestInvoke_SavedConfigurationAndExplicitOverrides(t *testing.T) {
	headers := map[string]any{"X-Saved": "original"}
	node, err := NewInvokeComponent(map[string]any{"method": "POST", "url": "https://public.example/saved", "body": "saved body", "headers": headers})
	if err != nil {
		t.Fatal(err)
	}
	headers["X-Saved"] = "mutated after construction"
	calls := 0
	ctx := context.WithValue(context.Background(), httpExecutorKey{}, httpExecutor(func(_ context.Context, method, target, body, contentType string, headers map[string]string) (*http.Response, error) {
		calls++
		if calls == 2 {
			if method != "GET" || target != "https://public.example/override" || body != "" || headers["X-Saved"] != "override" {
				t.Fatalf("override ignored: %s %s %q %v", method, target, body, headers)
			}
		} else if method != "POST" || target != "https://public.example/saved" || body != "saved body" || headers["X-Saved"] != "original" || contentType != "application/json" {
			t.Fatalf("saved configuration ignored: %s %s %q %v", method, target, body, headers)
		}
		return response(200, "ok"), nil
	}))
	if _, err = node.Invoke(ctx, nil); err != nil {
		t.Fatal(err)
	}
	if _, err = node.Invoke(ctx, map[string]any{"method": "GET", "url": "https://public.example/override", "body": "", "headers": map[string]any{"X-Saved": "override"}}); err != nil {
		t.Fatal(err)
	}
	if _, err = node.Invoke(ctx, nil); err != nil {
		t.Fatal(err)
	}
	if calls != 3 {
		t.Fatal(calls)
	}
}

func TestInvoke_ValidatesSavedConfiguration(t *testing.T) {
	for _, params := range []map[string]any{
		{"url": "file:///private/path"},
		{"method": "PATCH"},
		{"timeout": json.Number("1e1000")},
		{"proxy": "http://127.0.0.1:1"},
		{"headers": map[string]any{"X-Value": 1}},
	} {
		if _, err := NewInvokeComponent(params); err == nil {
			t.Fatalf("invalid defaults accepted: %v", params)
		}
	}
}

func TestLLM_ResolvesSupportedPromptReferences(t *testing.T) {
	state := canvas.NewCanvasState("run", "task")
	state.SetSys("query", "actual question")
	ctx := canvas.WithState(context.Background(), state)
	invoker := &stubInvoker{resp: &ChatInvokeResponse{Content: "answer"}}
	ctx = WithChatInvoker(ctx, invoker)
	node := NewLLMComponent(LLMParam{ModelID: "42", SystemPrompt: `{"format":"JSON"}`, UserPrompt: "Question: {{sys.query}}"})
	if _, err := node.Invoke(ctx, nil); err != nil {
		t.Fatal(err)
	}
	if invoker.captured.Messages[0].Content != `{"format":"JSON"}` || invoker.captured.Messages[1].Content != "Question: actual question" {
		t.Fatalf("prompt refs/literals changed: %+v", invoker.captured.Messages)
	}
	if _, err := node.Invoke(ctx, map[string]any{"user_prompt": "{{sys.missing}}"}); err == nil {
		t.Fatal("unresolved reference accepted")
	}
	if invoker.calls != 1 {
		t.Fatal("provider called with unresolved prompt")
	}
}

func TestAgent_ResolvesSupportedPromptReferences(t *testing.T) {
	state := canvas.NewCanvasState("run", "task")
	state.SetSys("query", "actual question")
	ctx := canvas.WithState(context.Background(), state)
	calls := 0
	withAgentRunner(t, func(_ context.Context, p AgentParam) (*schema.Message, error) {
		calls++
		if p.UserPrompt != "actual question" || p.SystemPrompt != `{"instructions":"literal"}` {
			t.Fatalf("unresolved agent prompt: %+v", p)
		}
		return &schema.Message{Content: "answer"}, nil
	})
	node := NewAgentComponent(AgentParam{ModelID: "42", UserPrompt: "{{sys.query}}", SystemPrompt: `{"instructions":"literal"}`})
	if _, err := node.Invoke(ctx, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := node.Invoke(ctx, map[string]any{"user_prompt": "{{sys.missing}}"}); err == nil {
		t.Fatal("unresolved reference accepted")
	}
	if calls != 1 {
		t.Fatal("agent called with unresolved prompt")
	}
}

func TestSwitch_RetainsSavedConditions(t *testing.T) {
	node, err := NewSwitchComponent(map[string]any{"conditions": []any{map[string]any{"op": "and", "to": "selected", "clauses": []any{map[string]any{"left": "{{sys.score}}", "op": ">", "right": json.Number("40")}}}}, "default": "fallback"})
	if err != nil {
		t.Fatal(err)
	}
	state := canvas.NewCanvasState("run", "task")
	state.SetSys("score", 42)
	ctx := canvas.WithState(context.Background(), state)
	out, err := node.Invoke(ctx, nil)
	if err != nil || out["_next"] != "selected" {
		t.Fatalf("saved route ignored: %v,%v", out, err)
	}
	out, err = node.Invoke(ctx, map[string]any{"conditions": []any{}, "default": "override"})
	if err != nil || out["_next"] != "override" {
		t.Fatal(out, err)
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
	out, err := node.Invoke(ctx, nil)
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
	if out, err := node.Invoke(context.Background(), map[string]any{"watermark_text": "DRAFT"}); out != nil || !errors.Is(err, ErrNotImplemented) {
		t.Fatalf("runtime option ignored: %v,%v", out, err)
	}
}
