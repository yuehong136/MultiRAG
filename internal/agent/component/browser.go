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

// Package component — Browser (T3, plan §2.11.3 row 15).
//
// Browser fetches public HTTP(S) content with the same DNS-pinned transport
// as tools. Optional summaries use the request's authorized model resolver.
// It returns content and metadata; storage uploads remain unavailable.
package component

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"multirag/internal/agent/runtime"
)

const (
	componentNameBrowser = "Browser"

	defaultBrowserTimeout = 30 * time.Second
)

// browserParam is the static configuration for a Browser node.
type browserParam struct {
	ModelID string `json:"model_id"` // optional LLM summarizer model
	URL     string `json:"url"`      // default target URL
	Prompt  string `json:"prompt"`   // optional summarization prompt
	Timeout int    `json:"timeout"`  // per-request timeout in seconds
}

// Update copies a fresh param map into the receiver.
func (p *browserParam) Update(conf map[string]any) error {
	if conf == nil {
		conf = map[string]any{}
	}
	p.ModelID, _ = stringFrom(conf, "model_id")
	p.URL, _ = conf["url"].(string)
	p.Prompt, _ = conf["prompt"].(string)
	// Preserve an explicitly-supplied timeout (including 0 / negative)
	// so Check() can reject bad values. Only reset to zero when the
	// caller omitted the field entirely.
	if v, ok := intFrom(conf, "timeout"); ok {
		p.Timeout = v
	} else {
		p.Timeout = 0
	}
	return nil
}

// Check validates the param. URL is optional at construction time —
// the resolved URL (param or input override) is checked at Invoke time
// so test fixtures can construct the component without a real URL.
func (p *browserParam) Check() error {
	if p.Timeout < 0 || p.Timeout > 30 {
		return &ParamError{Field: "timeout", Reason: "must be between 0 and 30"}
	}
	return nil
}

// AsDict returns the params as a plain map.
func (p *browserParam) AsDict() map[string]any {
	return map[string]any{
		"model_id": p.ModelID,
		"url":      p.URL,
		"prompt":   p.Prompt,
		"timeout":  p.Timeout,
	}
}

// BrowserComponent implements the Browser canvas node.
type BrowserComponent struct {
	name  string
	param browserParam
}

// NewBrowserComponent constructs a Browser from the DSL param map.
func NewBrowserComponent(params map[string]any) (Component, error) {
	p := &browserParam{}
	if err := p.Update(params); err != nil {
		return nil, fmt.Errorf("Browser: param update: %w", err)
	}
	if err := p.Check(); err != nil {
		return nil, fmt.Errorf("Browser: param check: %w", err)
	}
	return &BrowserComponent{
		name:  componentNameBrowser,
		param: *p,
	}, nil
}

// Name returns the registered component name.
func (b *BrowserComponent) Name() string { return b.name }

// Invoke fetches bounded content and optionally calls the selected model.
func (b *BrowserComponent) Invoke(ctx context.Context, inputs map[string]any) (map[string]any, error) {
	state, _, err := runtime.GetStateFromContext[*runtime.CanvasState](ctx)
	if err != nil {
		return nil, fmt.Errorf("Browser: %w", err)
	}
	if state == nil {
		return nil, errors.New("Browser: nil canvas state")
	}

	// Resolve URL: input override → state(file_ref) → param default.
	rawURL := b.param.URL
	if v, ok := inputs["url"].(string); ok && strings.TrimSpace(v) != "" {
		rawURL = v
	} else if ref, ok := inputs["file_ref"].(string); ok && ref != "" {
		// file_ref points at a stored path/url; for P4 we just echo it
		// back as the target URL (Phase 5 will resolve to a MinIO path).
		if v, err := state.GetVar(ref); err == nil && v != nil {
			if s, ok := v.(string); ok && s != "" {
				rawURL = s
			}
		}
	}
	if strings.TrimSpace(rawURL) == "" {
		return nil, &ParamError{Field: "url", Reason: "required (param or inputs.url)"}
	}
	// Resolve prompt override (input.prompt → param.prompt).
	prompt := b.param.Prompt
	if v, ok := inputs["prompt"].(string); ok && v != "" {
		prompt = v
	}

	timeout := defaultBrowserTimeout
	if b.param.Timeout > 0 {
		timeout = time.Duration(b.param.Timeout) * time.Second
	}

	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	resp, err := executeHTTP(ctx, http.MethodGet, rawURL, "", "", map[string]string{"Accept": "text/html,text/plain"})
	if err != nil {
		return nil, fmt.Errorf("Browser: %w", err)
	}
	bodyBytes, err := readHTTPBody(resp)
	if err != nil {
		return nil, fmt.Errorf("Browser: %w", err)
	}

	finalURL := rawURL
	if resp.Request != nil && resp.Request.URL != nil {
		finalURL = resp.Request.URL.String()
	}

	content := string(bodyBytes)
	modelID := b.param.ModelID
	if v, ok := stringFrom(inputs, "model_id"); ok && v != "" {
		modelID = v
	}
	if modelID != "" {
		if prompt == "" {
			return nil, &ParamError{Field: "prompt", Reason: "required for summarization"}
		}
		result, err := chatInvokerFor(ctx).Invoke(ctx, ChatInvokeRequest{ModelName: modelID, Messages: buildMessages(prompt, content)})
		if err != nil {
			return nil, fmt.Errorf("Browser summarize: %w", err)
		}
		if result == nil {
			return nil, errors.New("Browser: model returned no response")
		}
		content = result.Content
	}

	return map[string]any{
		"content":  content,
		"url":      finalURL,
		"status":   resp.StatusCode,
		"size":     len(bodyBytes),
		"model_id": modelID,
		"prompt":   prompt,
	}, nil
}

// Stream mirrors Invoke; Browser is a single-shot HTTP fetch.
func (b *BrowserComponent) Stream(ctx context.Context, inputs map[string]any) (<-chan map[string]any, error) {
	out, err := b.Invoke(ctx, inputs)
	if err != nil {
		return nil, err
	}
	ch := make(chan map[string]any, 1)
	ch <- out
	close(ch)
	return ch, nil
}

// Inputs returns parameter metadata.
func (b *BrowserComponent) Inputs() map[string]string {
	return map[string]string{
		"model_id": "Authorized tenant model ID used for a real summary call.",
		"url":      "Target URL; can be a {{...}} reference resolved upstream.",
		"prompt":   "Optional LLM prompt (e.g. \"summarize this page\"); used when model_id is set.",
		"timeout":  "Per-request timeout in seconds; default 30.",
	}
}

// Outputs returns the response surface.
func (b *BrowserComponent) Outputs() map[string]string {
	return map[string]string{
		"content":  "Fetched body or model summary; response body limited to 8 MiB.",
		"url":      "Final URL after redirects.",
		"status":   "HTTP status code (int).",
		"size":     "Body size in bytes (int).",
		"model_id": "Resolved LLM model id (empty when summarization is disabled).",
		"prompt":   "Resolved LLM prompt (echoed back for downstream nodes).",
	}
}

func init() {
	Register(componentNameBrowser, NewBrowserComponent)
}
