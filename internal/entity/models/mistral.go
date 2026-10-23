// Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
// Licensed under the Apache License, Version 2.0.
package models

import (
	"encoding/json"
	"fmt"
	"strings"
)

// MistralModel shares the hosted chat transport and strict indexed embeddings.
type MistralModel struct{ *openAIChatDriver }

func NewMistralModel(urls map[string]string, suffix URLSuffix) *MistralModel {
	d := newOpenAIChatDriver(urls, suffix, "mistral", openAIChatBody)
	d.parseContent = mistralContent
	d.waitForStreamEOF = true
	return &MistralModel{d}
}

func (m *MistralModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(m.httpClient, m.BaseURL, m.URLSuffix.Embedding, name, texts, api, config)
}

// Mistral reasoning models can return typed text/thinking parts in either a
// complete response or a stream delta. Unknown metadata parts carry no text.
func mistralContent(raw json.RawMessage) (string, string, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return "", "", nil
	}
	var text string
	if json.Unmarshal(raw, &text) == nil {
		return text, "", nil
	}
	var parts []struct {
		Type     string `json:"type"`
		Text     string `json:"text"`
		Thinking []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"thinking"`
	}
	if err := json.Unmarshal(raw, &parts); err != nil {
		return "", "", fmt.Errorf("mistral: invalid content: %w", err)
	}
	var answer, reasoning strings.Builder
	for _, part := range parts {
		switch part.Type {
		case "text":
			answer.WriteString(part.Text)
		case "thinking":
			for _, inner := range part.Thinking {
				if inner.Type == "text" {
					reasoning.WriteString(inner.Text)
				}
			}
		}
	}
	return answer.String(), reasoning.String(), nil
}

var _ ModelDriver = (*MistralModel)(nil)
