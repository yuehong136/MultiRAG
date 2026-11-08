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
	"io"
	"net/http"
	"strings"
	"testing"
)

func response(status int, body string) *http.Response {
	return &http.Response{StatusCode: status, Header: http.Header{"X-Test": []string{"ok"}}, Body: io.NopCloser(strings.NewReader(body))}
}
func TestInvoke_RequestAndResponse(t *testing.T) {
	for _, method := range []string{"GET", "POST", "PUT", "DELETE"} {
		t.Run(method, func(t *testing.T) {
			called := false
			execute := httpExecutor(func(ctx context.Context, verb, target, body, contentType string, headers map[string]string) (*http.Response, error) {
				called = true
				if verb != method || target != "https://public.example/path" || body != "payload" || headers["Authorization"] != "token" {
					t.Fatalf("request mismatch %s %s %s %v", verb, target, body, headers)
				}
				if _, ok := ctx.Deadline(); !ok {
					t.Fatal("request lacks timeout")
				}
				if (method == "POST" || method == "PUT") && contentType != "application/json" {
					t.Fatalf("content type %q", contentType)
				}
				return response(201, "body"), nil
			})
			ctx := context.WithValue(context.Background(), httpExecutorKey{}, execute)
			c, _ := NewInvokeComponent(nil)
			out, err := c.Invoke(ctx, map[string]any{"method": method, "url": "https://public.example/path", "body": "payload", "headers": map[string]any{"Authorization": "token"}})
			if err != nil || !called || out["body"] != "body" || out["status"] != 201 {
				t.Fatalf("result %v,%v", out, err)
			}
			if out["headers"].(map[string]string)["X-Test"] != "ok" {
				t.Fatal(out)
			}
		})
	}
}
func TestInvoke_RejectsUnsafeInputs(t *testing.T) {
	c, _ := NewInvokeComponent(nil)
	for _, input := range []map[string]any{
		{"method": "PATCH", "url": "http://127.0.0.1"},
		{"method": "GET"},
		{"method": "GET", "url": "http://127.0.0.1"},
		{"method": "GET", "url": "http://169.254.169.254/latest/meta-data"},
		{"method": "GET", "url": "file:///etc/passwd"},
		{"method": "GET", "url": "https://example.com", "proxy": "http://localhost:8080"},
	} {
		if out, err := c.Invoke(context.Background(), input); err == nil || out != nil {
			t.Fatalf("unsafe input succeeded %v -> %v,%v", input, out, err)
		}
	}
}
func TestInvoke_PropagatesHTTPFailure(t *testing.T) {
	c, _ := NewInvokeComponent(nil)
	expected := errors.New("transport failed")
	ctx := context.WithValue(context.Background(), httpExecutorKey{}, httpExecutor(func(context.Context, string, string, string, string, map[string]string) (*http.Response, error) {
		return nil, expected
	}))
	if _, err := c.Invoke(ctx, map[string]any{"method": "GET", "url": "https://example.com"}); !errors.Is(err, expected) {
		t.Fatal(err)
	}
}
