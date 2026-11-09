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
	"multirag/internal/agent/runtime"
	"net/http"
	"testing"

	"multirag/internal/agent/canvas"
)

func TestBrowser_FetchAndSummarize(t *testing.T) {
	c, err := NewBrowserComponent(map[string]any{"url": "https://8.8.8.8/page", "model_id": "42", "prompt": "Summarize"})
	if err != nil {
		t.Fatal(err)
	}
	state := canvas.NewCanvasState("run-browser", "task-browser")
	ctx := canvas.WithState(context.Background(), state)
	previous := http.DefaultTransport
	http.DefaultTransport = componentRoundTripper(func(r *http.Request) (*http.Response, error) {
		method, target := r.Method, r.URL.String()
		if method != "GET" || target != "https://8.8.8.8/page" {
			t.Fatal(method, target)
		}
		return response(200, "source page"), nil
	})
	t.Cleanup(func() { http.DefaultTransport = previous })
	out, err := c.Invoke(ctx, nil)
	if !errors.Is(err, runtime.ErrNotImplemented) || out != nil {
		t.Fatalf("summarization falsely succeeded: %v %v", out, err)
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
	previous := http.DefaultTransport
	http.DefaultTransport = componentRoundTripper(func(*http.Request) (*http.Response, error) {
		return nil, expected
	})
	t.Cleanup(func() { http.DefaultTransport = previous })
	if _, err := c.Invoke(ctx, map[string]any{"url": "https://8.8.8.8"}); !errors.Is(err, expected) {
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

func TestBrowser_FetchReturnsRealContent(t *testing.T) {
	previous := http.DefaultTransport
	http.DefaultTransport = componentRoundTripper(func(request *http.Request) (*http.Response, error) {
		if request.Method != "GET" || request.URL.String() != "https://8.8.8.8/content" {
			t.Fatal(request.Method, request.URL)
		}
		return response(200, "actual content"), nil
	})
	t.Cleanup(func() { http.DefaultTransport = previous })
	node, err := NewBrowserComponent(map[string]any{"url": "https://8.8.8.8/content"})
	if err != nil {
		t.Fatal(err)
	}
	ctx := canvas.WithState(context.Background(), canvas.NewCanvasState("fetch", "fetch"))
	out, err := node.Invoke(ctx, nil)
	if err != nil || out["content"] != "actual content" || out["size"] != len("actual content") || out["status"] != 200 {
		t.Fatal(out, err)
	}
}
