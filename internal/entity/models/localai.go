// Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
// Licensed under the Apache License, Version 2.0.
package models

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"maps"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

// LocalAIModel uses a tenant endpoint, optional authentication, and no media capability.
type LocalAIModel struct {
	*DummyModel
	httpClient        *http.Client
	streamIdleTimeout time.Duration
}

func NewLocalAIModel(urls map[string]string, suffix URLSuffix) *LocalAIModel {
	return &LocalAIModel{DummyModel: NewDummyModel(maps.Clone(urls), suffix), httpClient: &http.Client{Transport: providerModelTransport}, streamIdleTimeout: 60 * time.Second}
}
func (l *LocalAIModel) Name() string { return "LocalAI" }

// Request-scoped drivers share connections; credentials remain on requests.
var providerModelTransport = providerHTTPTransport()

// providerHTTPTransport retains dial and TLS deadlines with custom defaults.

func providerHTTPTransport() *http.Transport {
	var transport *http.Transport
	if standard, ok := http.DefaultTransport.(*http.Transport); ok && standard != nil {
		transport = standard.Clone()
	} else {
		transport = &http.Transport{Proxy: http.ProxyFromEnvironment, DialContext: (&net.Dialer{Timeout: 30 * time.Second, KeepAlive: 30 * time.Second}).DialContext, TLSHandshakeTimeout: 10 * time.Second, ExpectContinueTimeout: time.Second}
	}
	transport.MaxIdleConns = 100
	transport.MaxIdleConnsPerHost = 10
	transport.IdleConnTimeout = 90 * time.Second
	transport.ResponseHeaderTimeout = 60 * time.Second
	return transport
}

func localAIChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	body, err := openAIChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	if config != nil {
		if stream && config.Stream != nil && !*config.Stream {
			return nil, fmt.Errorf("LocalAI: stream must be true")
		}
		if config.Thinking != nil {
			body["enable_thinking"] = *config.Thinking
		}
		if config.Effort != nil && *config.Effort != "" {
			body["reasoning_effort"] = *config.Effort
		}
	}
	return body, nil
}
func (l *LocalAIModel) request(api *APIConfig, body any) (*http.Response, error) {
	var region *string
	if api != nil {
		region = api.Region
	}
	base, err := resolveModelBaseURL(l.BaseURL, region)
	if err != nil {
		return nil, err
	}
	if l.URLSuffix.Chat == "" {
		return nil, fmt.Errorf("LocalAI chat endpoint is missing")
	}
	data, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(requestContext(api), http.MethodPost, joinModelURL(base, l.URLSuffix.Chat), bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if api != nil && api.APIKey != nil && strings.TrimSpace(*api.APIKey) != "" {
		req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*api.APIKey))
	}
	resp, err := l.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("LocalAI chat failed (HTTP %d)", resp.StatusCode)
	}
	return resp, nil
}

type localAIMessage struct {
	Content          *string `json:"content"`
	ReasoningContent *string `json:"reasoning_content"`
	Reasoning        *string `json:"reasoning"`
	Thinking         *string `json:"thinking"`
}

func (m localAIMessage) reasoning() *string {
	for _, v := range []*string{m.ReasoningContent, m.Reasoning, m.Thinking} {
		if v != nil && *v != "" {
			return v
		}
	}
	return nil
}
func (l *LocalAIModel) ChatWithMessages(name string, api *APIConfig, messages []Message, config *ChatConfig) (*ChatResponse, error) {
	body, err := localAIChatBody(name, messages, config, false)
	if err != nil {
		return nil, err
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := l.request(local, body)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result struct {
		Choices []struct {
			Message localAIMessage `json:"message"`
		} `json:"choices"`
		Error json.RawMessage `json:"error"`
	}
	if err := decodeBoundedModelJSON(resp.Body, &result); err != nil {
		return nil, err
	}
	if len(result.Error) > 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("LocalAI provider error")
	}
	if len(result.Choices) == 0 || result.Choices[0].Message.Content == nil || strings.TrimSpace(*result.Choices[0].Message.Content) == "" {
		return nil, fmt.Errorf("LocalAI: no text answer")
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	message := result.Choices[0].Message
	return &ChatResponse{Answer: message.Content, ReasoningContent: message.reasoning()}, nil
}
func (l *LocalAIModel) ChatStreamlyWithSender(name, message *string, api *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if name == nil || message == nil {
		return fmt.Errorf("model and message are required")
	}
	return l.ChatStreamlyWithMessages(*name, []Message{{Role: "user", Content: *message}}, api, config, sender)
}
func (l *LocalAIModel) ChatStreamlyWithMessages(name string, messages []Message, api *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil {
		return fmt.Errorf("sender is required")
	}
	body, err := localAIChatBody(name, messages, config, true)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithCancel(requestContext(api))
	defer cancel()
	local := APIConfig{Context: ctx}
	if api != nil {
		local = *api
		local.Context = ctx
	}
	resp, err := l.request(&local, body)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	// Bound inactivity rather than the total duration of a working SSE response.
	idle := l.streamIdleTimeout
	if idle <= 0 {
		idle = 60 * time.Second
	}
	lastActive := time.Now()
	var mu sync.Mutex
	stopped, watchdogDone := make(chan struct{}), make(chan struct{})
	timedOut := false
	go func() {
		defer close(watchdogDone)
		ticker := time.NewTicker(max(idle/4, time.Millisecond))
		defer ticker.Stop()
		for {
			select {
			case <-stopped:
				return
			case <-ctx.Done():
				return
			case now := <-ticker.C:
				mu.Lock()
				expired := now.Sub(lastActive) >= idle
				if expired {
					timedOut = true
				}
				mu.Unlock()
				if expired {
					cancel()
					return
				}
			}
		}
	}()
	defer func() { close(stopped); <-watchdogDone }()
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	answered := false
	complete := func() error {
		if err := ctx.Err(); err != nil {
			return err
		}
		if !answered {
			return fmt.Errorf("LocalAI: no text answer")
		}
		done := "[DONE]"
		return sender(&done, nil)
	}
	for scanner.Scan() {
		mu.Lock()
		lastActive = time.Now()
		mu.Unlock()
		if err := ctx.Err(); err != nil {
			return err
		}
		line := scanner.Text()
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		data := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if data == "" {
			continue
		}
		if data == "[DONE]" {
			return complete()
		}
		var event struct {
			Choices []struct {
				Delta        localAIMessage `json:"delta"`
				FinishReason *string        `json:"finish_reason"`
			} `json:"choices"`
			Error json.RawMessage `json:"error"`
		}
		if err := json.Unmarshal([]byte(data), &event); err != nil {
			return fmt.Errorf("LocalAI invalid stream: %w", err)
		}
		if len(event.Error) > 0 && string(event.Error) != "null" {
			return fmt.Errorf("LocalAI stream provider error")
		}
		if len(event.Choices) == 0 {
			continue
		}
		delta := event.Choices[0].Delta
		if reasoning := delta.reasoning(); reasoning != nil {
			if err := sender(nil, reasoning); err != nil {
				return err
			}
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		if delta.Content != nil && *delta.Content != "" {
			answered = answered || strings.TrimSpace(*delta.Content) != ""
			if err := sender(delta.Content, nil); err != nil {
				return err
			}
		}
		if reason := event.Choices[0].FinishReason; reason != nil && *reason != "" {
			return complete()
		}
	}
	mu.Lock()
	expired := timedOut
	mu.Unlock()
	if expired {
		return fmt.Errorf("LocalAI stream idle timeout: %w", context.DeadlineExceeded)
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	return io.ErrUnexpectedEOF
}
func (l *LocalAIModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(l.httpClient, l.BaseURL, l.URLSuffix.Embedding, name, texts, api, config, embeddingHTTPOptions{optionalAPIKey: true})
}
func (l *LocalAIModel) Rerank(name *string, query string, texts []string, api *APIConfig, config *RerankConfig) (*RerankResponse, error) {
	return rerankHTTP(l.httpClient, l.BaseURL, l.URLSuffix.Rerank, name, query, texts, api, config, map[string]any{"return_documents": nil}, rerankHTTPOptions{optionalAPIKey: true})
}
func (l *LocalAIModel) ListModels(api *APIConfig) ([]string, error) {
	return listProviderModels(l.httpClient, l.BaseURL, l.URLSuffix.Models, api, false)
}
func (l *LocalAIModel) CheckConnection(api *APIConfig) error { _, err := l.ListModels(api); return err }
func (l *LocalAIModel) Balance(api *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("%w: LocalAI balance", ErrUnsupportedCapability)
}

var _ ModelDriver = (*LocalAIModel)(nil)
