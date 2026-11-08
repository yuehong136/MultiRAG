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

// interrupt_resume_test.go — unit tests for the eino interrupt
// wrappers. These exercise the helpers directly without spinning up a
// full eino runner (a separate integration test does that).

package canvas

import (
	"context"
	"errors"
	"testing"

	"github.com/cloudwego/eino/compose"
	"multirag/internal/agent/runtime"
)

// TestBuildInputSpec_BasicFields passes enable_tips/tips/inputs and
// expects all three plus the `kind` tag to surface in the result.
func TestBuildInputSpec_BasicFields(t *testing.T) {
	got := BuildInputSpec(map[string]any{
		"enable_tips": true,
		"tips":        "Please enter your name",
		"inputs":      map[string]any{"name": map[string]any{"type": "string"}},
	})

	for k, want := range map[string]any{
		"enable_tips": true,
		"tips":        "Please enter your name",
		"kind":        "user_fill_up",
	} {
		if got[k] != want {
			t.Errorf("BuildInputSpec[%q] = %v, want %v", k, got[k], want)
		}
	}
	if _, ok := got["inputs"]; !ok {
		t.Errorf("BuildInputSpec dropped the `inputs` key")
	}
}

// TestBuildInputSpec_NilSafe covers the nil params path (defensive).
func TestBuildInputSpec_NilSafe(t *testing.T) {
	got := BuildInputSpec(nil)
	if got == nil {
		t.Fatalf("BuildInputSpec(nil) returned nil; want empty map")
	}
	if got["kind"] != "user_fill_up" {
		t.Errorf("BuildInputSpec(nil) kind = %v, want user_fill_up", got["kind"])
	}
}

// TestIsInterruptError_NilSafe returns false on nil.
func TestIsInterruptError_NilSafe(t *testing.T) {
	if IsInterruptError(nil) {
		t.Errorf("IsInterruptError(nil) = true; want false")
	}
}

// TestIsInterruptError_ExcludesCancel confirms context cancellation
// errors are NOT classified as interrupt errors. Otherwise the
// Driver would emit `waiting_for_user` on user-cancel.
func TestIsInterruptError_ExcludesCancel(t *testing.T) {
	if IsInterruptError(context.Canceled) {
		t.Errorf("IsInterruptError(context.Canceled) = true; want false")
	}
	if IsInterruptError(context.DeadlineExceeded) {
		t.Errorf("IsInterruptError(context.DeadlineExceeded) = true; want false")
	}
}

// TestIsInterruptError_Positive covers a non-nil generic error.
// Plain errors are not interrupt signals — IsInterruptError must
// return false.
func TestIsInterruptError_PlainError(t *testing.T) {
	if IsInterruptError(errors.New("boom")) {
		t.Errorf("IsInterruptError(plain err) = true; want false")
	}
}

// TestExtractInterruptContexts_NilSafe covers the nil error case.
func TestExtractInterruptContexts_NilSafe(t *testing.T) {
	if got := ExtractInterruptContexts(nil); got != nil {
		t.Errorf("ExtractInterruptContexts(nil) = %v; want nil", got)
	}
}

// TestExtractInterruptContexts_PlainError covers the negative path:
// a plain error has no InterruptContexts.
func TestExtractInterruptContexts_PlainError(t *testing.T) {
	got := ExtractInterruptContexts(errors.New("boom"))
	if got != nil {
		t.Errorf("ExtractInterruptContexts(plain err) = %v; want nil", got)
	}
}

// TestFirstInterruptID_Empty covers the empty/nil case.
func TestFirstInterruptID_Empty(t *testing.T) {
	if got := FirstInterruptID(nil); got != "" {
		t.Errorf("FirstInterruptID(nil) = %q; want \"\"", got)
	}
	if got := FirstInterruptID([]*compose.InterruptCtx{}); got != "" {
		t.Errorf("FirstInterruptID([]) = %q; want \"\"", got)
	}
}

// TestFirstInterruptID_PicksFirst confirms it returns the first
// element's ID.
func TestFirstInterruptID_PicksFirst(t *testing.T) {
	got := FirstInterruptID([]*compose.InterruptCtx{
		{ID: "first"},
		{ID: "second"},
	})
	if got != "first" {
		t.Errorf("FirstInterruptID = %q; want \"first\"", got)
	}
}

// TestUserFillUpNodeBody_FirstCallInterrupts covers the first-call
// branch: the node must call compose.Interrupt and surface the
// resulting error. We pass a regular (non-resume) ctx and expect the
// call to return a non-nil error.
func TestUserFillUpNodeBody_FirstCallInterrupts(t *testing.T) {
	body := UserFillUpNodeBody("ufu_1", map[string]any{
		"enable_tips": true,
		"tips":        "hello",
	})
	_, err := body(context.Background(), map[string]any{"x": 1})
	if err == nil {
		t.Fatalf("UserFillUpNodeBody first call returned nil err; want interrupt signal")
	}
	if !IsInterruptError(err) {
		// We don't require exact wording — eino may wrap the signal
		// in internal types that don't expose the substring "interrupt"
		// in Error(). The robust check is IsInterruptError.
		t.Errorf("UserFillUpNodeBody first call error = %v; expected to be classified as interrupt", err)
	}
}

// TestAutoDiscoverUserFillUpIDs_Empty covers the nil canvas path.
func TestAutoDiscoverUserFillUpIDs_NilSafe(t *testing.T) {
	if got := AutoDiscoverUserFillUpIDs(nil); got != nil {
		t.Errorf("AutoDiscoverUserFillUpIDs(nil) = %v; want nil", got)
	}
}

// TestAutoDiscoverUserFillUpIDs_CaseInsensitive confirms the
// case-insensitive name match (UserFillUp vs userfillup vs USERFILLUP).
func TestAutoDiscoverUserFillUpIDs_CaseInsensitive(t *testing.T) {
	c := &Canvas{
		Components: map[string]CanvasComponent{
			"a": {Obj: CanvasComponentObj{ComponentName: "UserFillUp"}},
			"b": {Obj: CanvasComponentObj{ComponentName: "userfillup"}},
			"c": {Obj: CanvasComponentObj{ComponentName: "USERFILLUP"}},
			"d": {Obj: CanvasComponentObj{ComponentName: "LLM"}},     // not UserFillUp
			"e": {Obj: CanvasComponentObj{ComponentName: "Message"}}, // not UserFillUp
		},
	}
	got := AutoDiscoverUserFillUpIDs(c)
	if len(got) != 3 {
		t.Errorf("AutoDiscoverUserFillUpIDs = %v; want 3 entries (a, b, c)", got)
	}
}

func TestNativeUserFillUpRejectsDeclaredFiles(t *testing.T) {
	cases := map[string]any{
		"object": map[string]any{"upload": map[string]any{"type": "file", "value": nil}},
		"list":   []any{map[string]any{"name": "upload", "type": "FiLe[]"}},
		"nested": map[string]any{"uploads": map[string]any{"type": "array", "items": map[string]any{"type": " file-document "}}},
	}
	for name, inputs := range cases {
		t.Run(name, func(t *testing.T) {
			params := map[string]any{"inputs": inputs}
			graph := &Canvas{Components: map[string]CanvasComponent{"wait": {Obj: CanvasComponentObj{ComponentName: "UserFillUp", Params: params}}}}
			if _, err := Compile(context.Background(), graph); !errors.Is(err, runtime.ErrNotImplemented) {
				t.Fatalf("file graph compiled: %v", err)
			}
			body := UserFillUpNodeBody("wait", params)
			for _, ctx := range []context.Context{context.Background(), compose.ResumeWithData(context.Background(), "file-interrupt", map[string]any{"upload": "opaque-file-id"})} {
				if _, err := body(ctx, nil); !errors.Is(err, runtime.ErrNotImplemented) || IsInterruptError(err) {
					t.Fatalf("file body accepted interrupt/resume: %v", err)
				}
			}
		})
	}
}

func TestNativeUserFillUpKeepsOrdinaryFormOpaque(t *testing.T) {
	params := map[string]any{"inputs": map[string]any{"filename": map[string]any{"type": "string"}, "count": map[string]any{"type": "number"}}}
	if err := validateUserFillUpInputs(params); err != nil {
		t.Fatal(err)
	}
	if _, err := UserFillUpNodeBody("wait", params)(context.Background(), nil); !IsInterruptError(err) {
		t.Fatalf("ordinary form did not produce native interrupt: %v", err)
	}
}
