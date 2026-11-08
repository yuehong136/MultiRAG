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
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"

	"multirag/internal/agent/runtime"
	"time"

	agenttool "multirag/internal/agent/tool"
)

const componentNameInvoke = "Invoke"
const defaultInvokeTimeout = 30 * time.Second
const defaultInvokeContentCT = "application/json"
const maxInvokeResponseBody = 8 << 20

type httpExecutor func(context.Context, string, string, string, string, map[string]string) (*http.Response, error)
type httpExecutorKey struct{}

// executeHTTP keeps the controlled HTTP test boundary separate from the
// production transport, which pins DNS and blocks non-public addresses.
func executeHTTP(ctx context.Context, method, target, body, contentType string, headers map[string]string) (*http.Response, error) {
	if execute, ok := ctx.Value(httpExecutorKey{}).(httpExecutor); ok {
		return execute(ctx, method, target, body, contentType, headers)
	}
	helper := agenttool.NewHTTPHelper()
	return helper.DoUntrusted(ctx, method, target, body, contentType, headers)
}

func readHTTPBody(resp *http.Response) ([]byte, error) {
	if resp == nil || resp.Body == nil {
		return nil, errors.New("HTTP response body missing")
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxInvokeResponseBody+1))
	if err != nil {
		return nil, err
	}
	if len(body) > maxInvokeResponseBody {
		return nil, errors.New("HTTP response exceeds 8 MiB")
	}
	return body, nil
}

type InvokeComponent struct {
	name     string
	defaults map[string]any
}

var invokeKeys = []string{"method", "url", "body", "headers", "timeout", "proxy", "content_type"}

func validateInvokeOptions(values map[string]any) error {
	for _, key := range []string{"method", "url", "body", "proxy", "content_type"} {
		if value, present := values[key]; present {
			if _, ok := value.(string); !ok {
				return &ParamError{Field: key, Reason: "must be a string"}
			}
		}
	}
	if method, ok := values["method"].(string); ok && !runtime.VarRefPattern.MatchString(method) {
		switch strings.ToUpper(strings.TrimSpace(method)) {
		case http.MethodGet, http.MethodPost, http.MethodPut, http.MethodDelete:
		default:
			return fmt.Errorf("Invoke: invalid method %q", method)
		}
	}
	if target, ok := values["url"].(string); ok && target != "" && !runtime.VarRefPattern.MatchString(target) {
		parsed, err := url.Parse(target)
		if err != nil || parsed.Host == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") {
			return &ParamError{Field: "url", Reason: "must be an HTTP(S) URL"}
		}
	}
	if proxy, _ := values["proxy"].(string); proxy != "" {
		return fmt.Errorf("Invoke proxy: %w", ErrNotImplemented)
	}
	if _, present := values["timeout"]; present {
		value, ok := floatFrom(values, "timeout")
		if !ok || value <= 0 || value > 30 {
			return &ParamError{Field: "timeout", Reason: "must be a finite number greater than 0 and no more than 30"}
		}
	}
	if value, present := values["headers"]; present {
		switch headers := value.(type) {
		case map[string]string:
		case map[string]any:
			for _, value := range headers {
				if _, ok := value.(string); !ok {
					return &ParamError{Field: "headers", Reason: "must contain string values"}
				}
			}
		default:
			return &ParamError{Field: "headers", Reason: "must be a string map"}
		}
	}
	return nil
}

func NewInvokeComponent(params map[string]any) (Component, error) {
	if err := validateInvokeOptions(params); err != nil {
		return nil, err
	}
	defaults := map[string]any{"method": http.MethodGet}
	for _, key := range invokeKeys {
		if value, present := params[key]; present {
			defaults[key] = value
		}
	}
	// Own the nested header map so construction never borrows mutable caller data.
	if raw, present := defaults["headers"]; present {
		headers := map[string]string{}
		switch value := raw.(type) {
		case map[string]string:
			for k, v := range value {
				headers[k] = v
			}
		case map[string]any:
			for k, v := range value {
				headers[k] = v.(string)
			}
		}
		defaults["headers"] = headers
	}
	return &InvokeComponent{name: componentNameInvoke, defaults: defaults}, nil
}
func (i *InvokeComponent) Name() string { return i.name }
func (i *InvokeComponent) Invoke(ctx context.Context, inputs map[string]any) (map[string]any, error) {
	merged := make(map[string]any, len(i.defaults)+len(inputs))
	for key, value := range i.defaults {
		merged[key] = value
	}
	for _, key := range invokeKeys {
		if value, present := inputs[key]; present {
			merged[key] = value
		}
	}
	if err := validateInvokeOptions(merged); err != nil {
		return nil, err
	}
	for _, key := range []string{"method", "url", "body", "content_type"} {
		if text, ok := merged[key].(string); ok {
			resolved, err := resolvePrompt(ctx, text)
			if err != nil {
				return nil, fmt.Errorf("Invoke %s: %w", key, err)
			}
			merged[key] = resolved
		}
	}
	inputs = merged
	method, _ := inputs["method"].(string)
	method = strings.ToUpper(strings.TrimSpace(method))
	switch method {
	case http.MethodGet, http.MethodPost, http.MethodPut, http.MethodDelete:
	default:
		return nil, fmt.Errorf("Invoke: invalid method %q", method)
	}
	target, _ := inputs["url"].(string)
	if target == "" {
		return nil, errors.New("Invoke: url is required")
	}
	if proxy, _ := inputs["proxy"].(string); proxy != "" {
		return nil, fmt.Errorf("Invoke proxy: %w", ErrNotImplemented)
	}
	timeout := defaultInvokeTimeout
	if seconds, ok := floatFrom(inputs, "timeout"); ok {
		if seconds <= 0 || seconds > 30 {
			return nil, &ParamError{Field: "timeout", Reason: "must be between 0 and 30 seconds"}
		}
		timeout = time.Duration(seconds * float64(time.Second))
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	contentType, _ := inputs["content_type"].(string)
	if contentType == "" && (method == http.MethodPost || method == http.MethodPut) {
		contentType = defaultInvokeContentCT
	}
	body, _ := inputs["body"].(string)
	headers := map[string]string{}
	switch raw := inputs["headers"].(type) {
	case map[string]string:
		for key, value := range raw {
			headers[key] = value
		}
	case map[string]any:
		for key, value := range raw {
			headers[key] = value.(string)
		}
	}
	for key, value := range headers {
		resolved, err := resolvePrompt(ctx, value)
		if err != nil {
			return nil, fmt.Errorf("Invoke header: %w", err)
		}
		headers[key] = resolved
	}
	resp, err := executeHTTP(ctx, method, target, body, contentType, headers)
	if err != nil {
		return nil, fmt.Errorf("Invoke: %w", err)
	}
	payload, err := readHTTPBody(resp)
	if err != nil {
		return nil, fmt.Errorf("Invoke: %w", err)
	}
	outHeaders := make(map[string]string, len(resp.Header))
	for key, values := range resp.Header {
		if len(values) > 0 {
			outHeaders[key] = values[0]
		}
	}
	return map[string]any{"status": resp.StatusCode, "body": string(payload), "headers": outHeaders}, nil
}
func (i *InvokeComponent) Stream(ctx context.Context, inputs map[string]any) (<-chan map[string]any, error) {
	out, err := i.Invoke(ctx, inputs)
	if err != nil {
		return nil, err
	}
	ch := make(chan map[string]any, 1)
	ch <- out
	close(ch)
	return ch, nil
}
func (i *InvokeComponent) Inputs() map[string]string {
	return map[string]string{"method": "GET, POST, PUT or DELETE", "url": "Public HTTP(S) URL; redirects and proxies are disabled", "body": "Request body", "headers": "String request headers", "timeout": "Maximum 30 seconds", "content_type": "Request Content-Type"}
}
func (i *InvokeComponent) Outputs() map[string]string {
	return map[string]string{"status": "HTTP status code", "body": "Response body, maximum 8 MiB", "headers": "Response headers"}
}
func init() { Register(componentNameInvoke, NewInvokeComponent) }
