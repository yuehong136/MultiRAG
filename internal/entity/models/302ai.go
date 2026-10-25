package models

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"
)

type AI302Model struct{ *openAIChatDriver }

func NewAI302Model(urls map[string]string, suffix URLSuffix) *AI302Model {
	suffix.Chat = strings.Trim(strings.TrimSpace(suffix.Chat), "/")
	suffix.Models = strings.Trim(strings.TrimSpace(suffix.Models), "/")
	suffix.Embedding = strings.Trim(strings.TrimSpace(suffix.Embedding), "/")
	suffix.Rerank = strings.Trim(strings.TrimSpace(suffix.Rerank), "/")
	suffix.ASR = strings.Trim(strings.TrimSpace(suffix.ASR), "/")
	suffix.OCR = strings.Trim(strings.TrimSpace(suffix.OCR), "/")
	suffix.DocumentParse = strings.Trim(strings.TrimSpace(suffix.DocumentParse), "/")
	suffix.Task = strings.Trim(strings.TrimSpace(suffix.Task), "/")
	driver := newOpenAIChatDriver(urls, suffix, "302ai", ai302ChatBody)
	driver.accept = "application/json"
	driver.streamIdleTimeout = 60 * time.Second
	driver.strictStream, driver.terminalWithoutDelta = true, true
	return &AI302Model{driver}
}

func ai302ChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	// The selected method controls the wire mode, even with a conflicting option.
	body, err := openAIChatBody(strings.TrimSpace(name), messages, config, stream)
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

// Normalize copies so concurrent calls never mutate caller-owned model names.
func ai302ModelName(name *string) *string {
	if name == nil {
		return nil
	}
	value := strings.TrimSpace(*name)
	return &value
}

func (a *AI302Model) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	return embedHTTPWithConfig(a.httpClient, a.BaseURL, a.URLSuffix.Embedding, ai302ModelName(name), texts, api, config)
}

func (a *AI302Model) Rerank(name *string, query string, texts []string, api *APIConfig, config *RerankConfig) (*RerankResponse, error) {
	if len(texts) > 0 && strings.TrimSpace(query) == "" {
		return nil, fmt.Errorf("302ai: rerank query is required")
	}
	return rerankHTTP(a.httpClient, a.BaseURL, a.URLSuffix.Rerank, ai302ModelName(name), strings.TrimSpace(query), texts, api, config, map[string]any{"return_documents": nil})
}

func (a *AI302Model) TranscribeAudio(name *string, audio []byte, api *APIConfig, config *ASRConfig) (*ASRResponse, error) {
	return multipartASR(a.httpClient, a.BaseURL, a.URLSuffix.ASR, ai302ModelName(name), audio, api, config, mediaHTTPOptions{accept: "application/json"})
}

// 302.AI proxies the same Mistral document OCR contract, including data URLs.
func (a *AI302Model) OCRFile(name *string, content []byte, api *APIConfig, config *OCRConfig) (*OCRResponse, error) {
	return (&MistralModel{a.openAIChatDriver}).OCRFile(ai302ModelName(name), content, api, config)
}

func (a *AI302Model) OCRURL(name *string, documentURL string, api *APIConfig, config *OCRConfig) (*OCRResponse, error) {
	return (&MistralModel{a.openAIChatDriver}).OCRURL(ai302ModelName(name), strings.TrimSpace(documentURL), api, config)
}

func (a *AI302Model) ParseFile(name *string, content []byte, documentURL string, api *APIConfig) (*ParseTask, error) {
	return submitMinerUTask(a.httpClient, a.BaseURL, a.URLSuffix.DocumentParse, ai302ModelName(name), content, strings.TrimSpace(documentURL), api)
}

func (a *AI302Model) ShowTask(id string, api *APIConfig) (*ParseTask, error) {
	suffix := a.URLSuffix.Task
	if suffix == "" {
		suffix = a.URLSuffix.DocumentParse
	}
	return readMinerUTask(a.httpClient, a.BaseURL, suffix, strings.TrimSpace(id), api)
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
	if err := decodeBoundedModelJSON(resp.Body, &result); err != nil {
		if err := requestContext(local).Err(); err != nil {
			return nil, err
		}
		return nil, fmt.Errorf("invalid hosted model inventory")
	}
	if result.Data == nil || len(result.Error) > 0 && string(result.Error) != "null" {
		return nil, fmt.Errorf("invalid hosted model inventory")
	}
	if err := validateModelResponseCode(result.Code); err != nil {
		return nil, err
	}
	names, seen := make([]string, 0, len(*result.Data)), map[string]bool{}
	for _, item := range *result.Data {
		if item.ID == "" || strings.TrimSpace(item.ID) != item.ID || seen[item.ID] {
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
