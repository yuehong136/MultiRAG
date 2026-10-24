package models

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type ReplicateModel struct {
	*DummyModel
	httpClient   *http.Client
	pollInterval time.Duration
}

func NewReplicateModel(urls map[string]string, suffix URLSuffix) *ReplicateModel {
	return &ReplicateModel{NewDummyModel(maps.Clone(urls), suffix), &http.Client{Transport: providerModelTransport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, time.Second}
}

func (r *ReplicateModel) Name() string { return "replicate" }

type replicatePrediction struct {
	ID     string          `json:"id"`
	Status string          `json:"status"`
	Output json.RawMessage `json:"output"`
	Error  json.RawMessage `json:"error"`
	URLs   struct {
		Get    string `json:"get"`
		Stream string `json:"stream"`
	} `json:"urls"`
}

func replicateModelPart(part string) bool {
	if part == "" || part == "." || part == ".." {
		return false
	}
	for _, ch := range part {
		if !(ch >= 'a' && ch <= 'z' || ch >= 'A' && ch <= 'Z' || ch >= '0' && ch <= '9' || ch == '-' || ch == '_' || ch == '.') {
			return false
		}
	}
	return true
}

func (r *ReplicateModel) predictionEndpoint(name string, api *APIConfig) (string, string, error) {
	if name == "" || strings.TrimSpace(name) != name {
		return "", "", fmt.Errorf("replicate: model name is required")
	}
	model, version, versioned := strings.Cut(name, ":")
	parts := strings.Split(model, "/")
	if versioned || len(parts) == 1 {
		if !versioned {
			version = model
		} else if len(parts) != 2 || !replicateModelPart(parts[0]) || !replicateModelPart(parts[1]) {
			return "", "", fmt.Errorf("replicate: versioned model must be owner/name:version")
		}
		if _, err := hex.DecodeString(version); err != nil || len(version) != 64 {
			return "", "", fmt.Errorf("replicate: version must be a full 64-character hexadecimal ID")
		}
		endpoint, err := r.endpoint(r.URLSuffix.Chat, api)
		return endpoint, name, err
	}
	if len(parts) != 2 || !replicateModelPart(parts[0]) || !replicateModelPart(parts[1]) {
		return "", "", fmt.Errorf("replicate: official model must be owner/name")
	}
	if r.URLSuffix.Models == "" {
		return "", "", fmt.Errorf("replicate: model endpoint is missing")
	}
	endpoint, err := r.endpoint(strings.TrimRight(r.URLSuffix.Models, "/")+"/"+parts[0]+"/"+parts[1]+"/predictions", api)
	return endpoint, "", err
}

func (r *ReplicateModel) endpoint(suffix string, api *APIConfig) (string, error) {
	if api == nil || api.APIKey == nil || strings.TrimSpace(*api.APIKey) == "" {
		return "", fmt.Errorf("replicate: API key is required")
	}
	if suffix == "" {
		return "", fmt.Errorf("replicate: endpoint is missing")
	}
	base, err := resolveModelBaseURL(r.BaseURL, api.Region)
	if err != nil {
		return "", err
	}
	return joinModelURL(base, suffix), nil
}

func replicateInput(messages []Message, config *ChatConfig) (map[string]any, error) {
	if err := ValidateMessages(messages); err != nil {
		return nil, err
	}
	var systems, prompts []string
	nonSystem := 0
	for _, message := range messages {
		if message.Role != "system" {
			nonSystem++
		}
	}
	for _, message := range messages {
		content, ok := message.Content.(string)
		if !ok {
			parts, err := ContentParts(message.Content)
			if err != nil {
				return nil, fmt.Errorf("replicate: invalid message")
			}
			var text strings.Builder
			for _, part := range parts {
				if part["type"] != "text" {
					return nil, fmt.Errorf("%w: replicate chat accepts text only", ErrUnsupportedCapability)
				}
				text.WriteString(part["text"].(string))
			}
			content = text.String()
		}
		if message.Role == "system" {
			systems = append(systems, content)
		} else if nonSystem == 1 && message.Role == "user" {
			prompts = append(prompts, content)
		} else {
			prompts = append(prompts, message.Role+": "+content)
		}
	}
	if len(prompts) == 0 {
		return nil, fmt.Errorf("replicate: prompt is required")
	}
	input := map[string]any{"prompt": strings.Join(prompts, "\n")}
	if len(systems) > 0 {
		input["system_prompt"] = strings.Join(systems, "\n\n")
	}
	if config != nil {
		if config.MaxTokens != nil {
			input["max_new_tokens"] = *config.MaxTokens
		}
		if config.Temperature != nil {
			input["temperature"] = *config.Temperature
		}
		if config.TopP != nil {
			input["top_p"] = *config.TopP
		}
	}
	// Stop is model-specific and has no portable Replicate prompt contract.
	return input, nil
}

func (r *ReplicateModel) request(endpoint, method string, api *APIConfig, body any, wait, stream bool) (*http.Response, error) {
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return nil, fmt.Errorf("replicate: invalid request")
		}
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(requestContext(api), method, endpoint, reader)
	if err != nil {
		return nil, fmt.Errorf("replicate: invalid endpoint")
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*api.APIKey))
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if wait {
		req.Header.Set("Prefer", "wait=60")
	}
	if stream {
		req.Header.Set("Accept", "text/event-stream")
	}
	resp, err := r.httpClient.Do(req)
	if err != nil {
		if err := requestContext(api).Err(); err != nil {
			return nil, err
		}
		return nil, fmt.Errorf("replicate: request failed")
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		resp.Body.Close()
		return nil, fmt.Errorf("replicate: request failed (HTTP %d)", resp.StatusCode)
	}
	return resp, nil
}

func (r *ReplicateModel) prediction(endpoint, method string, api *APIConfig, body any, wait bool) (*replicatePrediction, error) {
	resp, err := r.request(endpoint, method, api, body, wait, false)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result replicatePrediction
	if err := decodeBoundedModelJSON(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("replicate: invalid prediction response")
	}
	if err := ValidateTaskID(result.ID); err != nil {
		return nil, fmt.Errorf("replicate: missing or invalid prediction ID")
	}
	if len(result.Error) > 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("replicate: prediction failed")
	}
	switch result.Status {
	case "starting", "processing", "succeeded":
	case "failed", "canceled":
		return nil, fmt.Errorf("replicate: prediction %s", result.Status)
	default:
		return nil, fmt.Errorf("replicate: unknown prediction status")
	}
	if err := requestContext(api).Err(); err != nil {
		return nil, err
	}
	return &result, nil
}

// Only the configured API origin and Replicate's documented stream hosts receive
// credentials. Redirects are disabled on this driver's client.
func replicateReturnedURL(raw, origin string, stream bool) error {
	u, err := url.Parse(raw)
	base, baseErr := url.Parse(origin)
	if err != nil || baseErr != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" || u.User != nil || u.Fragment != "" {
		return fmt.Errorf("replicate: invalid prediction URL")
	}
	if u.Scheme == base.Scheme && u.Host == base.Host {
		return nil
	}
	if stream && u.Scheme == "https" && (u.Host == "stream.replicate.com" || u.Host == "streaming.api.replicate.com") {
		return nil
	}
	return fmt.Errorf("replicate: unexpected prediction URL origin")
}

func (r *ReplicateModel) wait(prediction *replicatePrediction, origin string, api *APIConfig) (*replicatePrediction, error) {
	if prediction.Status == "succeeded" {
		return prediction, nil
	}
	if err := replicateReturnedURL(prediction.URLs.Get, origin, false); err != nil {
		return nil, err
	}
	id, endpoint := prediction.ID, prediction.URLs.Get
	interval := r.pollInterval
	if interval <= 0 {
		interval = time.Second
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-requestContext(api).Done():
			return nil, requestContext(api).Err()
		case <-ticker.C:
			next, err := r.prediction(endpoint, http.MethodGet, api, nil, false)
			if err != nil {
				return nil, err
			}
			if next.ID != id {
				return nil, fmt.Errorf("replicate: prediction ID mismatch")
			}
			if next.Status == "succeeded" {
				return next, nil
			}
		}
	}
}

func replicateAnswer(output json.RawMessage) (string, error) {
	var answer string
	if err := json.Unmarshal(output, &answer); err != nil {
		var parts []*string
		if err := json.Unmarshal(output, &parts); err != nil {
			return "", fmt.Errorf("replicate: output must be text or text fragments")
		}
		for _, part := range parts {
			if part == nil {
				return "", fmt.Errorf("replicate: missing output fragment")
			}
			answer += *part
		}
	}
	if strings.TrimSpace(answer) == "" {
		return "", fmt.Errorf("replicate: no text answer")
	}
	return answer, nil
}

func (r *ReplicateModel) start(name string, api *APIConfig, messages []Message, config *ChatConfig, stream bool) (*replicatePrediction, string, error) {
	endpoint, version, err := r.predictionEndpoint(name, api)
	if err != nil {
		return nil, "", err
	}
	input, err := replicateInput(messages, config)
	if err != nil {
		return nil, "", err
	}
	body := map[string]any{"input": input, "stream": stream}
	if version != "" {
		body["version"] = version
	}
	prediction, err := r.prediction(endpoint, http.MethodPost, api, body, !stream)
	return prediction, endpoint, err
}

func (r *ReplicateModel) ChatWithMessages(name string, api *APIConfig, messages []Message, config *ChatConfig) (*ChatResponse, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	prediction, origin, err := r.start(name, local, messages, config, false)
	if err != nil {
		return nil, err
	}
	prediction, err = r.wait(prediction, origin, local)
	if err != nil {
		return nil, err
	}
	answer, err := replicateAnswer(prediction.Output)
	if err != nil {
		return nil, err
	}
	return &ChatResponse{Answer: &answer}, nil
}

func (r *ReplicateModel) ChatStreamlyWithSender(name, message *string, api *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if name == nil || message == nil {
		return fmt.Errorf("replicate: model and message are required")
	}
	return r.ChatStreamlyWithMessages(*name, []Message{{Role: "user", Content: *message}}, api, config, sender)
}

func (r *ReplicateModel) ChatStreamlyWithMessages(name string, messages []Message, api *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if sender == nil || config != nil && config.Stream != nil && !*config.Stream {
		return fmt.Errorf("replicate: sender is required and stream must be true")
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	prediction, origin, err := r.start(name, local, messages, config, true)
	if err != nil {
		return err
	}
	if prediction.Status == "succeeded" || prediction.URLs.Stream == "" {
		prediction, err = r.wait(prediction, origin, local)
		if err != nil {
			return err
		}
		answer, err := replicateAnswer(prediction.Output)
		if err != nil {
			return err
		}
		if err := sender(&answer, nil); err != nil {
			return err
		}
	} else {
		if err := replicateReturnedURL(prediction.URLs.Stream, origin, true); err != nil {
			return err
		}
		// Bound the SSE body independently of the initial prediction call.
		ctx, stop := context.WithTimeout(requestContext(api), 10*time.Minute)
		defer stop()
		streamAPI := *local
		streamAPI.Context = ctx
		resp, err := r.request(prediction.URLs.Stream, http.MethodGet, &streamAPI, nil, false, true)
		if err != nil {
			return err
		}
		defer resp.Body.Close()
		terminal := errors.New("replicate terminal event")
		answered := false
		err = scanMediaSSE(resp, func(event string, data []byte) error {
			if err := ctx.Err(); err != nil {
				return err
			}
			switch event {
			case "output", "":
				text := string(data)
				answered = answered || strings.TrimSpace(text) != ""
				return sender(&text, nil)
			case "error":
				return fmt.Errorf("replicate: stream failed")
			case "done":
				var result map[string]json.RawMessage
				if err := json.Unmarshal(data, &result); err != nil || result == nil || len(result) != 0 || !answered {
					return fmt.Errorf("replicate: stream cancelled, failed, or has no answer")
				}
				return terminal
			default:
				return nil
			}
		})
		if !errors.Is(err, terminal) {
			if err == nil {
				return io.ErrUnexpectedEOF
			}
			return err
		}
	}
	if err := requestContext(api).Err(); err != nil {
		return err
	}
	done := "[DONE]"
	return sender(&done, nil)
}

func (r *ReplicateModel) ListModels(api *APIConfig) ([]string, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	endpoint, err := r.endpoint(r.URLSuffix.Models, local)
	if err != nil {
		return nil, err
	}
	resp, err := r.request(endpoint, http.MethodGet, local, nil, false, false)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result struct {
		Results *[]struct {
			Owner string `json:"owner"`
			Name  string `json:"name"`
		} `json:"results"`
		Error json.RawMessage `json:"error"`
	}
	if err := decodeBoundedModelJSON(resp.Body, &result); err != nil || result.Results == nil || len(result.Error) > 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("replicate: invalid model list")
	}
	names, seen := make([]string, 0, len(*result.Results)), map[string]bool{}
	for _, item := range *result.Results {
		name := item.Owner + "/" + item.Name
		if !replicateModelPart(item.Owner) || !replicateModelPart(item.Name) || seen[name] {
			return nil, fmt.Errorf("replicate: invalid or duplicate model ID")
		}
		seen[name] = true
		names = append(names, name)
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return names, nil
}

func (r *ReplicateModel) CheckConnection(api *APIConfig) error {
	_, err := r.ListModels(api)
	return err
}

var _ ModelDriver = (*ReplicateModel)(nil)
