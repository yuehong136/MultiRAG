package models

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"time"
)

// NVIDIAModel implements NVIDIA chat, embeddings, reranking and model discovery.
type NVIDIAModel struct{ *openAIChatDriver }

func NewNVIDIAModel(baseURL map[string]string, suffix URLSuffix) *NVIDIAModel {
	driver := newOpenAIChatDriver(baseURL, suffix, "nvidia", nvidiaChatBody)
	driver.splitReasoning = true
	driver.optionalDiscoveryKey = true
	return &NVIDIAModel{driver}
}

func nvidiaChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	body, err := openAIChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	if config == nil || config.Temperature == nil {
		delete(body, "temperature")
	}
	if config != nil {
		if config.Thinking != nil {
			mode := "disabled"
			if *config.Thinking {
				mode = "enabled"
			}
			body["thinking"] = map[string]string{"type": mode}
		}
		if stream && config.DoSample != nil {
			body["do_sample"] = *config.DoSample
		}
	}
	return body, nil
}

func (n *NVIDIAModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	inputType := "query"
	if config != nil && !config.Query {
		inputType = "passage"
	}
	local := APIConfig{}
	if api != nil {
		local = *api
	}
	ctx, cancel := context.WithTimeout(requestContext(api), 30*time.Second)
	defer cancel()
	local.Context = ctx
	return embedHTTPWithConfig(n.httpClient, n.BaseURL, n.URLSuffix.Embedding, name, texts, &local, config, embeddingHTTPOptions{
		fallbackRegion: true,
		parameters: map[string]any{
			"input_type":      inputType,
			"encoding_format": "float",
			"truncate":        "END",
		},
	})
}

// Rerank uses NIM's query/passages payload and preserves raw logit scores.
func (n *NVIDIAModel) Rerank(name *string, query string, texts []string, api *APIConfig, config *RerankConfig) (*RerankResponse, error) {
	top, err := RerankTopN(len(texts), config)
	if err != nil {
		return nil, err
	}
	if len(texts) == 0 {
		return &RerankResponse{Data: []RerankResult{}}, nil
	}
	if name == nil || strings.TrimSpace(*name) == "" {
		return nil, fmt.Errorf("nvidia: model name is required")
	}
	passages := make([]map[string]string, len(texts))
	for i, text := range texts {
		passages[i] = map[string]string{"text": text}
	}
	local := APIConfig{}
	if api != nil {
		local = *api
	}
	ctx, cancel := context.WithTimeout(requestContext(api), 30*time.Second)
	defer cancel()
	local.Context = ctx
	base, err := resolveModelBaseURL(n.BaseURL, local.Region)
	if err != nil {
		return nil, err
	}
	driver, suffix := n.openAIChatDriver, n.URLSuffix.Rerank
	// NVIDIA's hosted retrieval endpoints differ from self-hosted NIM /ranking.
	// Only translate the catalog default; explicit instance URLs/suffixes stay authoritative.
	if base == "https://integrate.api.nvidia.com/v1" && suffix == "ranking" {
		endpoint := ""
		switch *name {
		case "nvidia/nv-rerankqa-mistral-4b-v3":
			endpoint = "https://ai.api.nvidia.com/v1/retrieval/nvidia"
		case "nvidia/llama-3.2-nv-rerankqa-1b-v2":
			endpoint = "https://ai.api.nvidia.com/v1/retrieval/nvidia/llama-3_2-nv-rerankqa-1b-v2"
		}
		if endpoint != "" {
			driver = newOpenAIChatDriver(map[string]string{"default": endpoint}, n.URLSuffix, n.Name(), nvidiaChatBody)
			driver.httpClient = n.httpClient
			local.Region = nil
			suffix = "reranking"
		}
	}
	resp, err := driver.request(http.MethodPost, suffix, &local, map[string]any{
		"model": *name, "query": map[string]string{"text": query},
		"passages": passages, "truncate": "END", "top_n": top,
	})
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var raw struct {
		Rankings []struct {
			Index *int     `json:"index"`
			Logit *float64 `json:"logit"`
		} `json:"rankings"`
		Error json.RawMessage `json:"error"`
		Code  json.RawMessage `json:"code"`
	}
	decoder := json.NewDecoder(resp.Body)
	if err := decoder.Decode(&raw); err != nil {
		return nil, fmt.Errorf("nvidia: decode rerank: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return nil, fmt.Errorf("nvidia: trailing rerank response")
	}
	if len(raw.Error) > 0 && string(raw.Error) != "null" || len(raw.Code) > 0 && string(raw.Code) != "null" {
		return nil, fmt.Errorf("nvidia: rerank provider returned an error")
	}
	response := &RerankResponse{Data: make([]RerankResult, 0, len(raw.Rankings))}
	for _, result := range raw.Rankings {
		if result.Index == nil || result.Logit == nil {
			return nil, fmt.Errorf("nvidia: rerank result missing index/logit")
		}
		response.Data = append(response.Data, RerankResult{Index: *result.Index, RelevanceScore: *result.Logit})
	}
	if err := ValidateRerankResponse(response, len(texts), config); err != nil {
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	sort.SliceStable(response.Data, func(i, j int) bool { return response.Data[i].RelevanceScore > response.Data[j].RelevanceScore })
	return response, nil
}

var _ ModelDriver = (*NVIDIAModel)(nil)
