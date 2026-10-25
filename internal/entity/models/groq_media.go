package models

import (
	"fmt"
	"strings"
)

func groqChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	if err := validateHostedGeneration(config); err != nil {
		return nil, err
	}
	body, err := gatewayChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	lower := strings.ToLower(name)
	if strings.Contains(lower, "gpt-oss") {
		body["include_reasoning"] = true
		if config != nil && config.Effort != nil {
			switch *config.Effort {
			case "low", "medium", "high":
				body["reasoning_effort"] = *config.Effort
			default:
				return nil, fmt.Errorf("groq: invalid reasoning effort")
			}
		}
	} else if strings.Contains(lower, "qwen") || strings.Contains(lower, "deepseek") {
		body["reasoning_format"] = "parsed"
	}
	return body, nil
}

func (g *GroqModel) TranscribeAudio(name *string, audio []byte, api *APIConfig, config *ASRConfig) (*ASRResponse, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, format, err := openAIASRRequest(g.httpClient, g.BaseURL, g.URLSuffix.ASR, name, audio, local, config, false)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	return decodeAudioTranscription(resp, format, local)
}

func (g *GroqModel) AudioSpeech(name *string, text string, api *APIConfig, config *TTSConfig) (*TTSResponse, error) {
	body, format, err := openAISpeechBody(name, text, config, false)
	if err != nil {
		return nil, err
	}
	delete(body, "stream")
	if err := requireSpeechVoice(body); err != nil {
		return nil, err
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(g.httpClient, g.BaseURL, g.URLSuffix.TTS, local, body, "")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	result, err := readSpeechBytes(resp, format)
	if err != nil {
		return nil, err
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return result, nil
}

var _ ASRDriver = (*GroqModel)(nil)
var _ TTSDriver = (*GroqModel)(nil)
