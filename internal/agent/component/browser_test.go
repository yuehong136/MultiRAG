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
	"net/http"
	"testing"

	"multirag/internal/agent/canvas"
)

func TestBrowser_FetchAndSummarize(t *testing.T) {
	c, err := NewBrowserComponent(map[string]any{"url": "https://public.example/page", "model_id": "42", "prompt": "Summarize"})
	if err != nil {
		t.Fatal(err)
	}
	state := canvas.NewCanvasState("run-browser", "task-browser")
	ctx := canvas.WithState(context.Background(), state)
	ctx = context.WithValue(ctx, httpExecutorKey{}, httpExecutor(func(ctx context.Context, method, target, body, contentType string, headers map[string]string) (*http.Response, error) {
		if method != "GET" || target != "https://public.example/page" {
			t.Fatal(method, target)
		}
		return response(200, "source page"), nil
	}))
	invoker := &stubInvoker{resp: &ChatInvokeResponse{Content: "summary"}}
	ctx = WithChatInvoker(ctx, invoker)
	out, err := c.Invoke(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	if out["content"] != "summary" || out["size"] != len("source page") {
		t.Fatal(out)
	}
	if invoker.calls != 1 || invoker.captured.ModelName != "42" || invoker.captured.Messages[1].Content != "source page" {
		t.Fatalf("model not actually called: %+v", invoker.captured)
	}
}
func TestBrowser_RejectsPrivateURL(t *testing.T) {
	c, _ := NewBrowserComponent(nil)
	ctx := canvas.WithState(context.Background(), canvas.NewCanvasState("private", "private"))
	if out, err := c.Invoke(ctx, map[string]any{"url": "http://127.0.0.1:1"}); err == nil || out != nil {
		t.Fatalf("private address accepted %v,%v", out, err)
	}
}
func TestBrowser_HTTPFailure(t *testing.T) {
	c, _ := NewBrowserComponent(nil)
	ctx := canvas.WithState(context.Background(), canvas.NewCanvasState("failed", "failed"))
	expected := errors.New("timeout")
	ctx = context.WithValue(ctx, httpExecutorKey{}, httpExecutor(func(context.Context, string, string, string, string, map[string]string) (*http.Response, error) {
		return nil, expected
	}))
	if _, err := c.Invoke(ctx, map[string]any{"url": "https://public.example"}); !errors.Is(err, expected) {
		t.Fatal(err)
	}
}
func TestBrowser_MissingURL(t *testing.T) {
	c, _ := NewBrowserComponent(nil)
	ctx := canvas.WithState(context.Background(), canvas.NewCanvasState("missing", "missing"))
	if _, err := c.Invoke(ctx, nil); err == nil {
		t.Fatal("missing URL accepted")
	}
}
func TestBrowser_ParamCheck(t *testing.T) {
	if _, err := NewBrowserComponent(map[string]any{"timeout": -1}); err == nil {
		t.Fatal("negative timeout accepted")
	}
}
