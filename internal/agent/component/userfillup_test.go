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

package component

import (
	"context"
	"errors"
	"testing"

	"multirag/internal/agent/canvas"
)

// TestUserFillUp_RendersTips verifies the canonical happy path: a
// single {{name}} placeholder in the tips template is resolved against
// the inputs[name].value entry. The "tips" output key must hold the
// rendered string.
func TestUserFillUp_RendersTips(t *testing.T) {
	c, _ := New(componentNameUserFillUp, map[string]any{
		"enable_tips": true,
		"tips":        "Hello {{name}}",
	})
	state := canvas.NewCanvasState("run-1", "task-1")
	ctx := withStateForTest(context.Background(), state)

	out, err := c.Invoke(ctx, map[string]any{
		"inputs": map[string]any{
			"name": map[string]any{"value": "World"},
		},
	})
	if err != nil {
		t.Fatalf("Invoke: %v", err)
	}
	if got, _ := out["tips"].(string); got != "Hello World" {
		t.Errorf("tips: got %q, want %q", got, "Hello World")
	}
}

// TestUserFillUp_DisableTips asserts that turning tips off removes the
// "tips" key entirely from the output. The form-field passthrough still
// runs.
func TestUserFillUp_DisableTips(t *testing.T) {
	c, _ := New(componentNameUserFillUp, map[string]any{
		"enable_tips": false,
		"tips":        "Should not render",
	})
	state := canvas.NewCanvasState("run-2", "task-2")
	ctx := withStateForTest(context.Background(), state)

	out, err := c.Invoke(ctx, map[string]any{
		"inputs": map[string]any{
			"name": map[string]any{"value": "World"},
		},
	})
	if err != nil {
		t.Fatalf("Invoke: %v", err)
	}
	if _, ok := out["tips"]; ok {
		t.Errorf("tips key must be absent when enable_tips=false, got %v", out["tips"])
	}
	// Passthrough still applies.
	if got, _ := out["name"].(string); got != "World" {
		t.Errorf("name passthrough: got %q, want %q", got, "World")
	}
}

// TestUserFillUp_PassesThroughInputs asserts the multi-field
// passthrough contract: every non-file field appears in the output
// with its inner `value` extracted. This is the contract the downstream
// LLM/Retrieval nodes rely on to consume the form's answers.
func TestUserFillUp_PassesThroughInputs(t *testing.T) {
	c, _ := New(componentNameUserFillUp, map[string]any{"enable_tips": false})
	state := canvas.NewCanvasState("run-3", "task-3")
	ctx := withStateForTest(context.Background(), state)

	out, err := c.Invoke(ctx, map[string]any{
		"inputs": map[string]any{
			"q":     map[string]any{"value": "What is RAGFlow?"},
			"top_k": map[string]any{"value": 5},
			"deep":  map[string]any{"value": true},
		},
	})
	if err != nil {
		t.Fatalf("Invoke: %v", err)
	}
	if got, _ := out["q"].(string); got != "What is RAGFlow?" {
		t.Errorf("q: got %q, want %q", got, "What is RAGFlow?")
	}
	if got, _ := out["top_k"].(int); got != 5 {
		t.Errorf("top_k: got %v, want 5", out["top_k"])
	}
	if got, _ := out["deep"].(bool); !got {
		t.Errorf("deep: got %v, want true", out["deep"])
	}
}

func TestUserFillUp_RejectsFileInput(t *testing.T) {
	c, _ := New(componentNameUserFillUp, nil)
	state := canvas.NewCanvasState("run-file", "task-file")
	ctx := withStateForTest(context.Background(), state)
	out, err := c.Invoke(ctx, map[string]any{"inputs": map[string]any{"upload": map[string]any{"type": "file", "value": "file-1"}}})
	if !errors.Is(err, ErrNotImplemented) || out != nil {
		t.Fatalf("file input must not return fake success: %v, %v", out, err)
	}
}

// TestUserFillUp_ParamCheck covers the ParamBase surface used by the
// orchestrator: defaults, Update, Check, AsDict. This is the contract
// the registry's editor tool relies on when round-tripping configs.
func TestUserFillUp_ParamCheck(t *testing.T) {
	var p userFillUpParam
	if err := p.Update(map[string]any{}); err != nil {
		t.Fatalf("Update(empty): %v", err)
	}
	if !p.EnableTips {
		t.Error("Update with empty conf should default enable_tips=true")
	}
	if p.Tips != defaultUserFillUpTips {
		t.Errorf("Update with empty conf should default tips=%q, got %q", defaultUserFillUpTips, p.Tips)
	}
	if err := p.Check(); err != nil {
		t.Errorf("Check: %v", err)
	}
	d := p.AsDict()
	if d["enable_tips"] != true || d["tips"] != defaultUserFillUpTips || d["layout_recognize"] != "" {
		t.Errorf("AsDict: %v", d)
	}
}
