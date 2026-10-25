// Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
package models

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"maps"
	"net/http"
	"strings"
	"time"
)

// openAIChatDriver shares the HTTP and response contract of the hosted chat providers.
type openAIChatDriver struct {
	*DummyModel
	httpClient           *http.Client
	provider             string
	buildBody            func(string, []Message, *ChatConfig, bool) (map[string]interface{}, error)
	splitReasoning       bool
	openRouterReasoning  bool
	reasoningAlias       bool
	reasoningFallback    bool
	rootFinishReason     bool
	optionalDiscoveryKey bool
	parseContent         func(json.RawMessage) (string, string, error)
	waitForStreamEOF     bool
	inlineThink          bool
	rejectNonzeroCode    bool
	authHeader           string
	strictDiscovery      bool
	strictStream         bool
	sseEvents            bool
	terminalWithoutDelta bool
	azureContentFilter   bool
	streamIdleTimeout    time.Duration
}

// One pool is shared across request-scoped drivers; credentials stay on requests.
var openAIChatTransport = newOpenAIChatTransport()

func newOpenAIChatTransport() http.RoundTripper {
	defaultTransport, ok := http.DefaultTransport.(*http.Transport)
	var transport *http.Transport
	if ok && defaultTransport != nil {
		transport = defaultTransport.Clone()
	} else if !ok && http.DefaultTransport != nil {
		// Preserve caller routing, tracing and policy wrappers that cannot
		// be cloned as standard connection pools.
		return http.DefaultTransport
	} else {
		transport = &http.Transport{Proxy: http.ProxyFromEnvironment}
	}
	transport.MaxIdleConns = 100
	transport.MaxIdleConnsPerHost = 10
	transport.IdleConnTimeout = 90 * time.Second
	transport.ResponseHeaderTimeout = 60 * time.Second
	return transport
}

func newOpenAIChatDriver(baseURL map[string]string, suffix URLSuffix, provider string, buildBody func(string, []Message, *ChatConfig, bool) (map[string]interface{}, error)) *openAIChatDriver {
	return &openAIChatDriver{DummyModel: NewDummyModel(maps.Clone(baseURL), suffix), httpClient: &http.Client{Transport: openAIChatTransport}, provider: provider, buildBody: buildBody}
}
func (d *openAIChatDriver) Name() string { return d.provider }

func openAIChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	if strings.TrimSpace(name) == "" {
		return nil, fmt.Errorf("model name is required")
	}
	body, err := aliyunChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	delete(body, "enable_thinking")
	delete(body, "do_sample")
	return body, nil
}

func (d *openAIChatDriver) request(method, suffix string, config *APIConfig, body interface{}) (*http.Response, error) {
	if config == nil {
		config = &APIConfig{}
	}
	if (config.APIKey == nil || strings.TrimSpace(*config.APIKey) == "") && !(d.optionalDiscoveryKey && method == http.MethodGet && suffix == d.URLSuffix.Models) {
		return nil, fmt.Errorf("%s: API key is required", d.Name())
	}
	if suffix == "" {
		return nil, fmt.Errorf("openai-compatible: endpoint suffix is missing")
	}
	baseURL, err := resolveModelBaseURL(d.BaseURL, config.Region)
	if err != nil {
		return nil, err
	}
	key := ""
	if config.APIKey != nil {
		key = strings.TrimSpace(*config.APIKey)
	}
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(requestContext(config), method, joinModelURL(baseURL, suffix), reader)
	if err != nil {
		return nil, err
	}
	if key != "" {
		if d.authHeader != "" {
			req.Header.Set(d.authHeader, key)
		} else {
			req.Header.Set("Authorization", "Bearer "+key)
		}
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if fields, ok := body.(map[string]interface{}); ok && fields["stream"] == true {
		req.Header.Set("Accept", "text/event-stream")
	}
	resp, err := d.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("openai-compatible request: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("openai-compatible request failed (HTTP %d)", resp.StatusCode)
	}
	return resp, nil
}

func (d *openAIChatDriver) chat(name string, messages []Message, apiConfig *APIConfig, config *ChatConfig) (*ChatResponse, error) {
	body, err := d.buildBody(name, messages, config, false)
	if err != nil {
		return nil, err
	}
	local, cancel := modelCallConfig(apiConfig)
	defer cancel()
	resp, err := d.request(http.MethodPost, d.URLSuffix.Chat, local, body)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result struct {
		Choices []struct {
			FinishReason         *string         `json:"finish_reason"`
			ContentFilterResults json.RawMessage `json:"content_filter_results"`
			Message              struct {
				Content          json.RawMessage `json:"content"`
				ReasoningContent *string         `json:"reasoning_content"`
				Reasoning        *string         `json:"reasoning"`
			} `json:"message"`
		} `json:"choices"`
		Error json.RawMessage `json:"error"`
		Code  json.RawMessage `json:"code"`
	}
	if err := decodeBoundedModelJSON(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("openai-compatible: decode response: %w", err)
	}
	if len(result.Error) != 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("openai-compatible: provider returned an error")
	}
	if d.rejectNonzeroCode {
		if err := validateModelResponseCode(result.Code); err != nil {
			return nil, err
		}
	}
	if len(result.Choices) == 0 {
		return nil, fmt.Errorf("openai-compatible: no text answer")
	}
	if d.strictStream {
		if err := validateStrictChatFinish(result.Choices[0].FinishReason); err != nil {
			return nil, err
		}
	}
	if d.azureContentFilter {
		choice := result.Choices[0]
		if err := azureOpenAIFilterResult(choice.FinishReason, choice.ContentFilterResults, false); err != nil {
			return nil, err
		}
	}
	message := result.Choices[0].Message
	content, extraReasoning, err := d.decodeContent(message.Content)
	if err != nil {
		return nil, err
	}
	if d.inlineThink {
		var inline string
		content, inline = splitNovitaThink(content)
		if inline != "" {
			extraReasoning += inline
		}
	}
	answer, reasoning := &content, message.ReasoningContent
	if extraReasoning != "" {
		if reasoning != nil && *reasoning != "" {
			extraReasoning += "\n" + *reasoning
		}
		reasoning = &extraReasoning
	}
	if d.openRouterReasoning && message.Reasoning != nil {
		reasoning = message.Reasoning
	}
	if d.reasoningAlias && (reasoning == nil || *reasoning == "") {
		reasoning = message.Reasoning
	}
	if reasoning == nil && d.splitReasoning {
		class := ModelClassFromName(name)
		if config != nil && config.ModelClass != nil {
			class = strings.ToLower(*config.ModelClass)
		}
		reasoning, answer = GetThinkingAndAnswer(&class, answer)
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	if answer == nil || strings.TrimSpace(*answer) == "" {
		return nil, fmt.Errorf("openai-compatible: no text answer")
	}
	return &ChatResponse{Answer: answer, ReasoningContent: reasoning}, nil
}
func (d *openAIChatDriver) ChatWithMessages(name string, apiConfig *APIConfig, messages []Message, config *ChatConfig) (*ChatResponse, error) {
	return d.chat(name, messages, apiConfig, config)
}

func (d *openAIChatDriver) ChatStreamlyWithSender(name, message *string, apiConfig *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if name == nil || message == nil {
		return fmt.Errorf("model name and message are required")
	}
	return d.ChatStreamlyWithMessages(*name, []Message{{Role: "user", Content: *message}}, apiConfig, config, sender)
}

func (d *openAIChatDriver) ChatStreamlyWithMessages(name string, messages []Message, apiConfig *APIConfig, config *ChatConfig, sender func(*string, *string) error) (streamErr error) {
	if sender == nil {
		return fmt.Errorf("stream sender is required")
	}
	body, err := d.buildBody(name, messages, config, true)
	if err != nil {
		return err
	}
	var active func()
	if d.streamIdleTimeout > 0 {
		ctx, cancel := context.WithCancelCause(requestContext(apiConfig))
		local := APIConfig{Context: ctx}
		if apiConfig != nil {
			local = *apiConfig
			local.Context = ctx
		}
		apiConfig = &local
		idleError := fmt.Errorf("%s stream idle timeout: %w", d.Name(), context.DeadlineExceeded)
		timer := time.AfterFunc(d.streamIdleTimeout, func() { cancel(idleError) })
		active = func() { timer.Reset(d.streamIdleTimeout) }
		defer func() {
			timer.Stop()
			if context.Cause(ctx) == idleError {
				streamErr = idleError
			}
			cancel(nil)
		}()
	}
	resp, err := d.request(http.MethodPost, d.URLSuffix.Chat, apiConfig, body)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if active != nil {
		resp.Body = &activeModelStream{ReadCloser: resp.Body, active: active}
	}
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	receivedAnswer, finished := false, false
	extractor := &novitaThinkExtractor{}
	emit := func(content, reasoning string) error {
		if err := requestContext(apiConfig).Err(); err != nil {
			return err
		}
		if reasoning != "" {
			if err := sender(nil, &reasoning); err != nil {
				return err
			}
		}
		if err := requestContext(apiConfig).Err(); err != nil {
			return err
		}
		if content != "" {
			if err := sender(&content, nil); err != nil {
				return err
			}
			receivedAnswer = receivedAnswer || strings.TrimSpace(content) != ""
		}
		return nil
	}
	complete := func() error {
		if err := requestContext(apiConfig).Err(); err != nil {
			return err
		}
		if d.inlineThink {
			if tail := extractor.Flush(); tail != nil {
				if err := emit(tail.content, tail.reasoning); err != nil {
					return err
				}
			}
		}
		if !receivedAnswer {
			return fmt.Errorf("openai-compatible: stream returned no text answer")
		}
		done := "[DONE]"
		return sender(&done, nil)
	}
	for {
		data, ok, err := nextOpenAIStreamData(scanner, d.sseEvents)
		if err != nil {
			return err
		}
		if !ok {
			break
		}
		if err := requestContext(apiConfig).Err(); err != nil {
			return err
		}

		if data == "[DONE]" {
			return complete()
		}
		if d.azureContentFilter {
			metadata, err := azureOpenAIStreamFrame([]byte(data))
			if err != nil {
				return err
			}
			if metadata {
				continue
			}
		}

		var event struct {
			Choices      []*openAIStreamChoice `json:"choices"`
			Error        json.RawMessage       `json:"error"`
			Usage        json.RawMessage       `json:"usage"`
			Code         json.RawMessage       `json:"code"`
			FinishReason *string               `json:"finish_reason"`
		}
		if err := json.Unmarshal([]byte(data), &event); err != nil {
			return fmt.Errorf("openai-compatible: decode stream: %w", err)
		}
		if len(event.Error) != 0 && string(event.Error) != "null" {
			return fmt.Errorf("openai-compatible: provider stream error")
		}
		if d.rejectNonzeroCode {
			if err := validateModelResponseCode(event.Code); err != nil {
				return err
			}
		}
		if d.strictStream && d.rootFinishReason {
			if err := validateStrictChatFinish(event.FinishReason); err != nil {
				return err
			}
		}
		if len(event.Choices) == 0 {
			if d.rootFinishReason && event.FinishReason != nil && *event.FinishReason != "" {
				return complete()
			}
			if len(event.Usage) > 0 && string(event.Usage) != "null" {
				continue
			}
			return fmt.Errorf("openai-compatible: stream event has no choices")
		}
		choice := event.Choices[0]
		if d.strictStream && choice != nil {
			if err := validateStrictChatFinish(choice.FinishReason); err != nil {
				return err
			}
		}
		if choice == nil || choice.Delta == nil {
			if d.strictStream && !(d.terminalWithoutDelta && choice != nil && choice.FinishReason != nil && *choice.FinishReason != "") {
				return fmt.Errorf("openai-compatible: invalid stream choice or delta")
			}
			if choice == nil {
				continue
			}
			choice.Delta = &openAIStreamDelta{}
		}
		delta := choice.Delta
		if d.openRouterReasoning && delta.RouterReasoning != nil {
			delta.Reasoning = delta.RouterReasoning
		}
		if delta.Reasoning != nil && *delta.Reasoning != "" {
			if err := sender(nil, delta.Reasoning); err != nil {
				return err
			}
		}
		if d.reasoningAlias && delta.RouterReasoning != nil && *delta.RouterReasoning != "" && (!d.reasoningFallback || delta.Reasoning == nil || *delta.Reasoning == "") {
			if err := requestContext(apiConfig).Err(); err != nil {
				return err
			}
			if err := sender(nil, delta.RouterReasoning); err != nil {
				return err
			}
		}
		if err := requestContext(apiConfig).Err(); err != nil {
			return err
		}
		content, reasoning, err := d.decodeContent(delta.Content)
		if err != nil {
			return err
		}
		if reasoning != "" {
			if err := sender(nil, &reasoning); err != nil {
				return err
			}
		}
		if d.inlineThink {
			for _, segment := range extractor.Feed(content) {
				if err := emit(segment.content, segment.reasoning); err != nil {
					return err
				}
			}
		} else if err := emit(content, ""); err != nil {
			return err
		}

		finish := choice.FinishReason
		if d.rootFinishReason && (finish == nil || *finish == "") {
			finish = event.FinishReason
		}
		if finish != nil && *finish != "" {
			if d.waitForStreamEOF {
				finished = true
				continue
			}
			if !receivedAnswer {
				return fmt.Errorf("openai-compatible: stream returned no text answer")
			}
			if err := requestContext(apiConfig).Err(); err != nil {
				return err
			}
			done := "[DONE]"
			return sender(&done, nil)
		}
	}
	if err := requestContext(apiConfig).Err(); err != nil {
		return err
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	if d.waitForStreamEOF && finished {
		return complete()
	}
	return io.ErrUnexpectedEOF
}

type openAIStreamDelta struct {
	Content         json.RawMessage `json:"content"`
	Reasoning       *string         `json:"reasoning_content"`
	RouterReasoning *string         `json:"reasoning"`
}

func validateStrictChatFinish(reason *string) error {
	if reason == nil || *reason == "" || *reason == "stop" || *reason == "length" {
		return nil
	}
	return fmt.Errorf("model chat terminated unsuccessfully or with an unsupported reason")
}

type openAIStreamChoice struct {
	FinishReason *string            `json:"finish_reason"`
	Delta        *openAIStreamDelta `json:"delta"`
}

func (d *openAIChatDriver) ListModels(config *APIConfig) ([]string, error) {
	local, cancel := modelCallConfig(config)
	defer cancel()
	resp, err := d.request(http.MethodGet, d.URLSuffix.Models, local, nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if d.strictDiscovery {
		names, err := strictProviderModelNames(resp.Body)
		if err == nil {
			err = requestContext(local).Err()
		}
		return names, err
	}
	var result struct {
		Data *[]struct {
			ID string `json:"id"`
		} `json:"data"`
		Error json.RawMessage `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, err
	}
	if len(result.Error) != 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("openai-compatible: model list error")
	}
	if result.Data == nil {
		return nil, fmt.Errorf("openai-compatible: missing model list")
	}
	names := make([]string, 0, len(*result.Data))
	for _, item := range *result.Data {
		if strings.TrimSpace(item.ID) == "" {
			return nil, fmt.Errorf("openai-compatible: invalid model ID")
		}
		names = append(names, item.ID)
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return names, nil
}

// activeModelStream bounds inactivity even when a provider stalls within an SSE line.
type activeModelStream struct {
	io.ReadCloser
	active func()
}

func (s *activeModelStream) Read(raw []byte) (int, error) {
	n, err := s.ReadCloser.Read(raw)
	if n > 0 {
		s.active()
	}
	return n, err
}

func rejectModelRedirect(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }

// These capabilities are deliberately unavailable in this driver batch.
func (d *openAIChatDriver) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	return nil, fmt.Errorf("openai-compatible: embedding is not implemented")
}
func (d *openAIChatDriver) Rerank(name *string, query string, texts []string, api *APIConfig, rerankConfig *RerankConfig) (*RerankResponse, error) {
	return nil, fmt.Errorf("openai-compatible: rerank is not implemented")
}
func (d *openAIChatDriver) Balance(api *APIConfig) (map[string]interface{}, error) {
	return nil, fmt.Errorf("openai-compatible: balance is not implemented")
}
func (d *openAIChatDriver) CheckConnection(api *APIConfig) error {
	_, err := d.ListModels(api)
	return err
}

var _ ModelDriver = (*openAIChatDriver)(nil)

func validateModelResponseCode(raw json.RawMessage) error {
	if len(raw) == 0 {
		return nil
	}
	var code *int
	if err := json.Unmarshal(raw, &code); err != nil || code == nil || *code != 0 {
		return fmt.Errorf("openai-compatible: invalid response code or provider error")
	}
	return nil
}

// decodeContent preserves the string-only contract unless a provider explicitly
// opts into its structured response format.
func (d *openAIChatDriver) decodeContent(raw json.RawMessage) (string, string, error) {
	if d.parseContent != nil {
		return d.parseContent(raw)
	}
	if len(raw) == 0 || string(raw) == "null" {
		return "", "", nil
	}
	var content string
	if err := json.Unmarshal(raw, &content); err != nil {
		return "", "", fmt.Errorf("openai-compatible: invalid content: %w", err)
	}
	return content, "", nil
}
