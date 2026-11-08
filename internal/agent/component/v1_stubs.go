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

// Generate adapts the v1 prompt names to the authorized LLM component.
// Unsupported v1 nodes remain unregistered, so compilation fails explicitly.
package component

import (
	"context"
	"fmt"
)

const componentNameGenerate = "Generate"

type GenerateComponent struct{ inner *LLMComponent }

func NewGenerateComponent(params map[string]any) (Component, error) {
	if cite, _ := params["cite"].(bool); cite {
		return nil, fmt.Errorf("Generate citations: %w", ErrNotImplemented)
	}
	if n, ok := intFrom(params, "message_history_window_size"); ok && n > 0 {
		return nil, fmt.Errorf("Generate message history: %w", ErrNotImplemented)
	}
	p, err := buildLLMParamFromV1Params(params)
	if err != nil {
		return nil, err
	}
	return &GenerateComponent{inner: NewLLMComponent(p)}, nil
}
func (g *GenerateComponent) Name() string { return componentNameGenerate }
func (g *GenerateComponent) Invoke(ctx context.Context, inputs map[string]any) (map[string]any, error) {
	return g.inner.Invoke(ctx, inputs)
}
func (g *GenerateComponent) Stream(ctx context.Context, inputs map[string]any) (<-chan map[string]any, error) {
	return g.inner.Stream(ctx, inputs)
}
func (g *GenerateComponent) Inputs() map[string]string {
	return map[string]string{"llm_id": "Authorized tenant model ID", "prompt": "User prompt", "temperature": "Sampling temperature", "max_tokens": "Output token limit"}
}
func (g *GenerateComponent) Outputs() map[string]string { return g.inner.Outputs() }
func buildLLMParamFromV1Params(p map[string]any) (LLMParam, error) {
	if err := validateLLMOptions(p); err != nil {
		return LLMParam{}, err
	}
	out := mergeLLMParam(LLMParam{}, p)
	if prompt, ok := stringFrom(p, "prompt"); ok {
		out.UserPrompt = prompt
	}
	return out, nil
}
func init() { Register(componentNameGenerate, NewGenerateComponent) }
