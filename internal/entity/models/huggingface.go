package models

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
)

// HuggingFaceModel uses the shared chat transport and the HF inference vector format.
type HuggingFaceModel struct{ *openAIChatDriver }

func NewHuggingFaceModel(baseURL map[string]string, suffix URLSuffix) *HuggingFaceModel {
	return &HuggingFaceModel{newOpenAIChatDriver(baseURL, suffix, "huggingface", huggingFaceChatBody)}
}

func huggingFaceChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	body, err := openAIChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	if config == nil || config.Temperature == nil {
		if stream {
			delete(body, "temperature")
		} else {
			body["temperature"] = 0.6
		}
	}
	if config != nil && config.Thinking != nil {
		mode := "disabled"
		if *config.Thinking {
			mode = "enabled"
		}
		body["thinking"] = map[string]string{"type": mode}
	}
	if stream && config != nil && config.DoSample != nil {
		body["do_sample"] = *config.DoSample
	}
	return body, nil
}

func (h *HuggingFaceModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	if config != nil && config.Dimension < 0 {
		return nil, fmt.Errorf("embedding dimension must be non-negative")
	}
	if len(texts) == 0 {
		return []EmbeddingData{}, nil
	}
	if name == nil || strings.TrimSpace(*name) == "" {
		return nil, fmt.Errorf("huggingface: model name is required")
	}
	// Model IDs may contain an organization separator, but cannot change the URL path.
	parts := strings.Split(*name, "/")
	for _, part := range parts {
		if part == "" || part == "." || part == ".." {
			return nil, fmt.Errorf("huggingface: invalid model ID")
		}
	}
	base, err := resolveModelBaseURL(h.BaseURL, apiRegion(api))
	if err != nil {
		return nil, err
	}
	parsed, err := url.Parse(base)
	if err != nil {
		return nil, err
	}
	// The router's chat API lives under /v1; inference lives beside it.
	parsed.Path = strings.TrimSuffix(strings.TrimRight(parsed.Path, "/"), "/v1")
	local, cancel := modelCallConfig(api)
	defer cancel()
	driver := newOpenAIChatDriver(map[string]string{"default": parsed.String()}, h.URLSuffix, h.Name(), huggingFaceChatBody)
	driver.httpClient = h.httpClient
	if h.URLSuffix.Embedding == "" {
		return nil, fmt.Errorf("huggingface: embedding endpoint is not configured")
	}
	for i, part := range parts {
		parts[i] = url.PathEscape(part)
	}
	// Use the already-resolved region while preserving per-call credentials/context.
	resolved := *local
	resolved.Region = nil
	resp, err := driver.request(http.MethodPost, strings.TrimRight(h.URLSuffix.Embedding, "/")+"/"+strings.Join(parts, "/"), &resolved, map[string]interface{}{"inputs": texts})
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var raw [][]*float64
	decoder := json.NewDecoder(resp.Body)
	if err := decoder.Decode(&raw); err != nil {
		return nil, fmt.Errorf("huggingface: decode embeddings: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return nil, fmt.Errorf("huggingface: trailing embedding response")
	}
	vectors := make([][]float64, len(raw))
	for i, row := range raw {
		vectors[i] = make([]float64, len(row))
		for j, value := range row {
			if value == nil {
				return nil, fmt.Errorf("huggingface: null embedding value")
			}
			vectors[i][j] = *value
		}
	}
	if err := ValidateEmbeddings(vectors, len(texts)); err != nil {
		return nil, err
	}
	if config != nil && config.Dimension > 0 {
		for _, vector := range vectors {
			if len(vector) != config.Dimension {
				return nil, fmt.Errorf("embedding dimension does not match request")
			}
		}
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return embeddingData(vectors), nil
}

func apiRegion(api *APIConfig) *string {
	if api == nil {
		return nil
	}
	return api.Region
}

var _ ModelDriver = (*HuggingFaceModel)(nil)
