package models

import (
	"strings"
	"time"
)

// QiniuModel implements chat and live discovery. Other capabilities remain unavailable.
type QiniuModel struct{ *openAIChatDriver }

func NewQiniuModel(urls map[string]string, suffix URLSuffix) *QiniuModel {
	d := newGatewayChatDriver(urls, suffix, "qiniu")
	d.buildBody = qiniuChatBody
	d.sseEvents = true
	d.streamIdleTimeout = 60 * time.Second
	d.httpClient.CheckRedirect = rejectModelRedirect
	return &QiniuModel{d}
}
func (q *QiniuModel) NewInstance(urls map[string]string) ModelDriver {
	return NewQiniuModel(urls, q.URLSuffix)
}
func qiniuChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	body, err := validatedHostedChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	if config == nil || config.Temperature == nil {
		body["temperature"] = 1
	}
	applyQiniuThinkingConfig(body, name, config)
	return body, nil
}

var qiniuQwenThinkingModels = map[string]struct{}{
	"qwen3-next-80b-a3b-thinking":   {},
	"qwen3-235b-a22b-thinking-2507": {},
	"qwen3-max-2026-01-23":          {},
	"qwen-turbo":                    {},
	"qwen3-32b":                     {},
	"qwen3-30b-a3b":                 {},
	"qwen3-30b-a3b-thinking-2507":   {},
	"qwen3.5-397b-a17b":             {},
	"qwen/qwen3.6-plus":             {},
	"qwen/qwen3.7-max":              {},
	"qwen/qwen3.6-27b":              {},
	"qwen3.5-35b-a3b":               {},
	"qwen3-vl-30b-a3b-thinking":     {},
}

var qiniuThinkingModels = map[string]struct{}{
	"deepseek/deepseek-v4-flash":               {},
	"deepseek/deepseek-v4-pro":                 {},
	"moonshotai/kimi-k2.6":                     {},
	"z-ai/glm-5.1":                             {},
	"z-ai/glm-5":                               {},
	"minimax/minimax-m2.7":                     {},
	"minimax/minimax-m2.5":                     {},
	"minimax/minimax-m2.5-highspeed":           {},
	"kimi-k2-thinking":                         {},
	"z-ai/glm-4.6":                             {},
	"deepseek/deepseek-v3.2-251201":            {},
	"deepseek/deepseek-v3.2-exp-thinking":      {},
	"deepseek/deepseek-v3.1-terminus-thinking": {},
	"deepseek-r1-0528":                         {},
	"deepseek-r1":                              {},
	"doubao-seed-1.6-flash":                    {},
	"doubao-seed-1.6":                          {},
	"doubao-seed-2.0-pro":                      {},
	"doubao-seed-2.0-lite":                     {},
	"doubao-seed-2.0-mini":                     {},
	"doubao-seed-2.0-code":                     {},
	"minimax-m1":                               {},
	"glm-4.5":                                  {},
	"glm-4.5-air":                              {},
	"tencent/hy3-preview":                      {},
}

func applyQiniuThinkingConfig(reqBody map[string]interface{}, modelName string, chatModelConfig *ChatConfig) {
	if chatModelConfig == nil || chatModelConfig.Thinking == nil {
		return
	}

	lowerModelName := strings.ToLower(modelName)
	if _, ok := qiniuQwenThinkingModels[lowerModelName]; ok {
		enableThinking := *chatModelConfig.Thinking
		if chatModelConfig.Effort != nil && strings.ToLower(*chatModelConfig.Effort) == "none" {
			enableThinking = false
		}
		reqBody["enable_thinking"] = enableThinking
		return
	}

	if _, ok := qiniuThinkingModels[lowerModelName]; !ok {
		return
	}

	thinkingType := "disabled"
	if *chatModelConfig.Thinking {
		thinkingType = "enabled"
		effort := "high"
		if chatModelConfig.Effort != nil && *chatModelConfig.Effort != "" {
			effort = strings.ToLower(*chatModelConfig.Effort)
		}

		switch effort {
		case "none":
			thinkingType = "disabled"
		case "default":
			reqBody["reasoning_effort"] = "high"
		case "low", "medium", "high":
			reqBody["reasoning_effort"] = effort
		case "max":
			reqBody["reasoning_effort"] = "high"
		default:
			reqBody["reasoning_effort"] = effort
		}
	}

	reqBody["thinking"] = map[string]interface{}{
		"type": thinkingType,
	}
}

var _ ModelDriver = (*QiniuModel)(nil)
