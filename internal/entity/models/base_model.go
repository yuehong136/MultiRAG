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

package models

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type BaseModel struct {
	BaseURL          map[string]string
	URLSuffix        URLSuffix
	httpClient       *http.Client
	AllowEmptyAPIKey bool
}

func (b *BaseModel) APIConfigCheck(apiConfig *APIConfig) error {
	if b.AllowEmptyAPIKey {
		return nil
	}

	if apiConfig == nil || apiConfig.ApiKey == nil || strings.TrimSpace(*apiConfig.ApiKey) == "" {
		return fmt.Errorf("api key is required")
	}

	return nil
}

// BearerAuth returns the Bearer token for Authorization header,
// or empty string if apiConfig or its ApiKey is nil/empty.
func BearerAuth(apiConfig *APIConfig) string {
	if apiConfig == nil || apiConfig.ApiKey == nil {
		return ""
	}
	key := strings.TrimSpace(*apiConfig.ApiKey)
	if key == "" {
		return ""
	}
	return fmt.Sprintf("Bearer %s", key)
}

func (b *BaseModel) GetBaseURL(apiConfig *APIConfig) (string, error) {
	var baseURL string
	if apiConfig != nil && apiConfig.BaseURL != nil && *apiConfig.BaseURL != "" {
		baseURL = strings.TrimSuffix(*apiConfig.BaseURL, "/")
	} else {
		region := "default"
		hasRegion := false
		if apiConfig != nil && apiConfig.Region != nil {
			region = *apiConfig.Region
			hasRegion = true
		}
		var ok bool
		baseURL, ok = b.BaseURL[region]
		if !ok || baseURL == "" {
			if (!hasRegion || region == "") && b.BaseURL != nil {
				baseURL = b.BaseURL["default"]
			}
			if baseURL == "" {
				return "", fmt.Errorf("no base URL configured for selected region")
			}
		}
	}
	endpoint, err := url.Parse(baseURL)
	if err != nil || endpoint.Host == "" || (endpoint.Scheme != "http" && endpoint.Scheme != "https") || endpoint.User != nil || endpoint.Fragment != "" || strings.TrimSpace(baseURL) != baseURL {
		return "", fmt.Errorf("invalid provider endpoint")
	}
	return baseURL, nil
}

// ParseSSEStream reads the body of an OpenAI-compatible Server-Sent Events
// response and calls onEvent for each successfully-parsed JSON payload.
// A malformed JSON payload after "data:" returns an error wrapped as
// "invalid SSE event" so the caller cannot silently swallow truncated or
// corrupted streams.
func ParseSSEStream[T any](r io.Reader, onEvent func(event T) error) (done bool, err error) {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)
	pending := ""
	for scanner.Scan() {
		line := scanner.Text()
		if strings.TrimSpace(line) == "" && pending != "" {
			var event T
			if err := json.Unmarshal([]byte(pending), &event); err != nil {
				return false, fmt.Errorf("invalid SSE event: %w", err)
			}
			if err := onEvent(event); err != nil {
				return false, err
			}
			pending = ""
			continue
		}
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		data := strings.TrimSpace(line[5:])
		if data == "" {
			continue
		}
		if pending != "" {
			pending += "\n" + data
			if len(pending) > 1024*1024 {
				return false, fmt.Errorf("SSE event exceeds limit")
			}
			continue
		}
		if data == "[DONE]" {
			return true, nil
		}
		var event T
		if err := json.Unmarshal([]byte(data), &event); err != nil {
			pending = data
			continue
		}
		if err := onEvent(event); err != nil {
			return false, err
		}
	}
	if err := scanner.Err(); err != nil {
		return false, err
	}
	if pending != "" {
		return false, fmt.Errorf("invalid SSE event: %w", io.ErrUnexpectedEOF)
	}
	return false, nil
}

// ParseSSEStreamTolerant is like ParseSSEStream but silently skips
// malformed JSON payloads. Use this only for drivers whose upstream is
// known to interleave invalid frames the test suite documents as safe
// to ignore.
func ParseSSEStreamTolerant[T any](r io.Reader, onEvent func(event T) error) (done bool, err error) {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		data := strings.TrimSpace(line[5:])
		if data == "" {
			continue
		}
		if data == "[DONE]" {
			return true, nil
		}
		var event T
		if err := json.Unmarshal([]byte(data), &event); err != nil {
			continue
		}
		if err := onEvent(event); err != nil {
			return false, err
		}
	}
	return false, scanner.Err()
}

// ParseListModel Parse model list. Empty/whitespace IDs are skipped so
// upstream typos do not surface as blank entries in the UI.
func ParseListModel(modelList ModelList) []ListModelResponse {
	var models []ListModelResponse
	pm := GetProviderManager()
	for _, model := range modelList.Models {
		modelName := strings.TrimSpace(model.ID)
		if modelName == "" {
			continue
		}
		var modelResponse ListModelResponse
		var modelEntity *Model
		if pm != nil {
			modelEntity = pm.GetModelByNameOrAlias(modelName)
		}
		if model.OwnedBy != "" {
			modelName = modelName + "@" + model.OwnedBy
		}
		modelResponse.Name = modelName
		if modelEntity != nil {
			if modelEntity.MaxDimension != nil {
				value := *modelEntity.MaxDimension
				modelResponse.MaxDimension = &value
			}
			if modelEntity.MaxTokens != nil {
				value := *modelEntity.MaxTokens
				modelResponse.MaxTokens = &value
			}
			if modelEntity.Dimensions != nil {
				modelResponse.Dimensions = append([]int{}, modelEntity.Dimensions...)
			}
			if modelEntity.ModelTypes != nil {
				modelResponse.ModelTypes = append([]string{}, modelEntity.ModelTypes...)
			}
			if modelEntity.Thinking != nil {
				value := *modelEntity.Thinking
				modelResponse.Thinking = &value
			}
		}

		models = append(models, modelResponse)
	}
	return models
}

// NewDriverHTTPClient returns an *http.Client with the standard connection-pool
func NewDriverHTTPClient() *http.Client {
	var t *http.Transport
	if dt, ok := http.DefaultTransport.(*http.Transport); ok && dt != nil {
		t = dt.Clone()
	} else if http.DefaultTransport != nil {
		if dt, ok := http.DefaultTransport.(*http.Transport); !ok || dt != nil {
			return &http.Client{Transport: http.DefaultTransport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
		}
		t = &http.Transport{Proxy: http.ProxyFromEnvironment, DialContext: (&net.Dialer{Timeout: 30 * time.Second, KeepAlive: 30 * time.Second}).DialContext, TLSHandshakeTimeout: 10 * time.Second, ExpectContinueTimeout: time.Second, ForceAttemptHTTP2: true}
	} else {
		t = &http.Transport{Proxy: http.ProxyFromEnvironment, DialContext: (&net.Dialer{Timeout: 30 * time.Second, KeepAlive: 30 * time.Second}).DialContext, TLSHandshakeTimeout: 10 * time.Second, ExpectContinueTimeout: time.Second, ForceAttemptHTTP2: true}
	}
	t.MaxIdleConns = 100
	t.MaxIdleConnsPerHost = 10
	t.IdleConnTimeout = 90 * time.Second
	t.DisableCompression = false
	t.ResponseHeaderTimeout = 60 * time.Second
	return &http.Client{Transport: t, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
}

// PostJSONRequest marshals body to JSON, creates a POST request to url
func PostJSONRequest(ctx context.Context, client *http.Client, url, auth string, body map[string]interface{}) (*http.Response, error) {
	data, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewBuffer(data))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if auth != "" {
		req.Header.Set("Authorization", auth)
	}
	return client.Do(req)
}

// ReadErrorBody reads all bytes from r and returns them as a string suitable
func ReadErrorBody(r io.Reader) string {
	b, _ := io.ReadAll(r)
	return string(b)
}
