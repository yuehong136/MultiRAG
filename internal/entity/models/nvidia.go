package models

import (
	"context"
	"time"
)

// NVIDIAModel implements NVIDIA chat, embeddings and instance model discovery.
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

var _ ModelDriver = (*NVIDIAModel)(nil)
