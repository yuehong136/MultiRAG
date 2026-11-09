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
			execute := componentRoundTripper(func(r *http.Request) (*http.Response, error) {
				ctx, verb, target, contentType := r.Context(), r.Method, r.URL.String(), r.Header.Get("Content-Type")
				raw, _ := io.ReadAll(r.Body)
				body := string(raw)
				headers := map[string]string{"Authorization": r.Header.Get("Authorization")}
				called = true
				if verb != method || target != "https://8.8.8.8/path" || body != "payload" || headers["Authorization"] != "token" {
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
			previous := http.DefaultTransport
			http.DefaultTransport = execute
			t.Cleanup(func() { http.DefaultTransport = previous })
			ctx := context.Background()
			c, _ := NewInvokeComponent(nil)
			out, err := c.Invoke(ctx, map[string]any{"method": method, "url": "https://8.8.8.8/path", "body": "payload", "headers": map[string]any{"Authorization": "token"}})
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
		{"method": "GET", "url": "https://8.8.8.8", "proxy": "http://localhost:8080"},
	} {
		if out, err := c.Invoke(context.Background(), input); err == nil || out != nil {
			t.Fatalf("unsafe input succeeded %v -> %v,%v", input, out, err)
		}
	}
}
func TestInvoke_PropagatesHTTPFailure(t *testing.T) {
	c, _ := NewInvokeComponent(nil)
	expected := errors.New("transport failed")
	ctx := context.Background()
	previous := http.DefaultTransport
	http.DefaultTransport = componentRoundTripper(func(*http.Request) (*http.Response, error) {
		return nil, expected
	})
	t.Cleanup(func() { http.DefaultTransport = previous })
	if _, err := c.Invoke(ctx, map[string]any{"method": "GET", "url": "https://8.8.8.8"}); !errors.Is(err, expected) {
		t.Fatal(err)
	}
}

type componentRoundTripper func(*http.Request) (*http.Response, error)

func (f componentRoundTripper) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
