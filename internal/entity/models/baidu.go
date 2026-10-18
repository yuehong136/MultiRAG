package models

import "strings"

// BaiduModel implements Qianfan chat, embeddings, reranking and model discovery.
type BaiduModel struct{ *openAIChatDriver }

func NewBaiduModel(baseURL map[string]string, suffix URLSuffix) *BaiduModel {
	return &BaiduModel{newOpenAIChatDriver(baseURL, suffix, "baidu", baiduChatBody)}
}

func baiduChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	body, err := openAIChatBody(name, messages, config, stream)
	if err != nil || config == nil {
		return body, err
	}
	if stream && config.DoSample != nil {
		body["do_sample"] = *config.DoSample
	}
	if config.Thinking == nil {
		return body, nil
	}
	lowerName := strings.ToLower(name)
	if strings.HasPrefix(lowerName, "qwen") || strings.HasPrefix(lowerName, "ernie") {
		body["enable_thinking"] = *config.Thinking
		return body, nil
	}
	mode := "disabled"
	if *config.Thinking {
		mode = "enabled"
		if strings.Contains(lowerName, "deepseek-v4") {
			effort := "high"
			if config.Effort != nil {
				effort = *config.Effort
			}
			switch effort {
			case "none", "low", "medium":
				mode = "disabled"
			case "high", "default":
				body["reasoning_effort"] = "high"
			default:
				body["reasoning_effort"] = effort
			}
		}
	}
	body["thinking"] = map[string]string{"type": mode}
	return body, nil
}

func (b *BaiduModel) ChatWithMessages(name string, api *APIConfig, messages []Message, config *ChatConfig) (*ChatResponse, error) {
	response, err := b.openAIChatDriver.ChatWithMessages(name, api, messages, config)
	if err != nil {
		return nil, err
	}
	if response.ReasoningContent != nil {
		reason := strings.TrimPrefix(*response.ReasoningContent, "\n")
		response.ReasoningContent = &reason
	}
	return response, nil
}

func (b *BaiduModel) Encode(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([][]float64, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	return encodeHTTP(b.httpClient, b.BaseURL, b.URLSuffix.Embedding, name, texts, local)
}

func (b *BaiduModel) Rerank(name *string, query string, texts []string, api *APIConfig, config *RerankConfig) (*RerankResponse, error) {
	return rerankHTTP(b.httpClient, b.BaseURL, b.URLSuffix.Rerank, name, query, texts, api, config, map[string]any{"return_documents": nil})
}

var _ ModelDriver = (*BaiduModel)(nil)
