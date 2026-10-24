package models

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
)

type AI302Model struct{ *openAIChatDriver }

func NewAI302Model(urls map[string]string, suffix URLSuffix) *AI302Model {
	return &AI302Model{newOpenAIChatDriver(urls, suffix, "302ai", ai302ChatBody)}
}

func ai302ChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	if stream && config != nil && config.Stream != nil && !*config.Stream {
		return nil, fmt.Errorf("302ai: stream must be true")
	}
	body, err := openAIChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	if config != nil {
		if config.Thinking != nil {
			mode := "disabled"
			if *config.Thinking {
				mode = "enabled"
			}
			body["thinking"] = map[string]string{"type": mode}
		}
		if config.Effort != nil {
			body["reasoning"] = map[string]string{"effort": *config.Effort}
		}
		if stream && config.DoSample != nil {
			body["do_sample"] = *config.DoSample
		}
	}
	return body, nil
}

func (a *AI302Model) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(a.httpClient, a.BaseURL, a.URLSuffix.Embedding, name, texts, api, config)
}

func (a *AI302Model) Rerank(name *string, query string, texts []string, api *APIConfig, config *RerankConfig) (*RerankResponse, error) {
	if len(texts) > 0 && strings.TrimSpace(query) == "" {
		return nil, fmt.Errorf("302ai: rerank query is required")
	}
	return rerankHTTP(a.httpClient, a.BaseURL, a.URLSuffix.Rerank, name, query, texts, api, config, map[string]any{"return_documents": nil})
}

func (a *AI302Model) TranscribeAudio(name *string, audio []byte, api *APIConfig, config *ASRConfig) (*ASRResponse, error) {
	return multipartASR(a.httpClient, a.BaseURL, a.URLSuffix.ASR, name, audio, api, config)
}

// 302.AI proxies the same Mistral document OCR contract, including data URLs.
func (a *AI302Model) OCRFile(name *string, content []byte, api *APIConfig, config *OCRConfig) (*OCRResponse, error) {
	return (&MistralModel{a.openAIChatDriver}).OCRFile(name, content, api, config)
}

func (a *AI302Model) OCRURL(name *string, documentURL string, api *APIConfig, config *OCRConfig) (*OCRResponse, error) {
	return (&MistralModel{a.openAIChatDriver}).OCRURL(name, documentURL, api, config)
}

func (a *AI302Model) ParseFile(name *string, content []byte, documentURL string, api *APIConfig) (*ParseTask, error) {
	return submitMinerUTask(a.httpClient, a.BaseURL, a.URLSuffix.DocumentParse, name, content, documentURL, api)
}

func (a *AI302Model) ShowTask(id string, api *APIConfig) (*ParseTask, error) {
	suffix := a.URLSuffix.Task
	if suffix == "" {
		suffix = a.URLSuffix.DocumentParse
	}
	return readMinerUTask(a.httpClient, a.BaseURL, suffix, id, api)
}

func (a *AI302Model) ListTasks(*APIConfig) ([]ParseTask, error) {
	return nil, fmt.Errorf("%w: 302ai task listing", ErrUnsupportedCapability)
}

// These hosted providers use authenticated OpenAI inventories, with no request
// body and strict IDs. Missing inventories never become an empty success.
func listStrictHostedModels(driver *openAIChatDriver, api *APIConfig) ([]string, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := driver.request(http.MethodGet, driver.URLSuffix.Models, local, nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var result struct {
		Data *[]struct {
			ID string `json:"id"`
		} `json:"data"`
		Error json.RawMessage `json:"error"`
		Code  json.RawMessage `json:"code"`
	}
	if err := decodeBoundedModelJSON(resp.Body, &result); err != nil || result.Data == nil || len(result.Error) > 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("invalid hosted model inventory")
	}
	if err := validateModelResponseCode(result.Code); err != nil {
		return nil, err
	}
	names, seen := make([]string, 0, len(*result.Data)), map[string]bool{}
	for _, item := range *result.Data {
		if strings.TrimSpace(item.ID) == "" || seen[item.ID] {
			return nil, fmt.Errorf("invalid or duplicate hosted model ID")
		}
		seen[item.ID] = true
		names = append(names, item.ID)
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return names, nil
}

func (a *AI302Model) ListModels(api *APIConfig) ([]string, error) {
	return listStrictHostedModels(a.openAIChatDriver, api)
}

func (a *AI302Model) CheckConnection(api *APIConfig) error { _, err := a.ListModels(api); return err }

var _ ModelDriver = (*AI302Model)(nil)
var _ ASRDriver = (*AI302Model)(nil)
var _ OCRDriver = (*AI302Model)(nil)
var _ OCRURLDriver = (*AI302Model)(nil)
var _ FileParser = (*AI302Model)(nil)
var _ TaskReader = (*AI302Model)(nil)
