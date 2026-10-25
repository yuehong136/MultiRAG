package models

import (
	"encoding/base64"
	"fmt"
	"strings"
)

func (o *OpenRouterModel) AudioSpeech(name *string, text string, api *APIConfig, config *TTSConfig) (*TTSResponse, error) {
	body, format, err := speechInput(name, text, config)
	if err != nil {
		return nil, err
	}
	delete(body, "format")
	delete(body, "audio_setting")
	body["model"] = *name
	body["input"] = text
	body["response_format"] = format
	if config != nil && config.Voice != "" {
		body["voice"] = config.Voice
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(o.httpClient, o.BaseURL, o.URLSuffix.TTS, local, body, "")
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

var _ TTSDriver = (*OpenRouterModel)(nil)

func (o *OpenRouterModel) TranscribeAudio(name *string, audio []byte, api *APIConfig, config *ASRConfig) (*ASRResponse, error) {
	if name == nil || strings.TrimSpace(*name) == "" {
		return nil, fmt.Errorf("ASR model required")
	}
	if err := ValidateMediaBytes(audio); err != nil {
		return nil, err
	}
	if err := ValidateASRConfig(config); err != nil {
		return nil, err
	}
	var params map[string]any
	format := "wav"
	if config != nil {
		params = config.Params
		if config.Format != "" {
			format = config.Format
		}
	}
	body, err := CloneMediaParams(params)
	if err != nil {
		return nil, err
	}
	if _, ok := body["input_audio"]; ok {
		return nil, fmt.Errorf("ASR params cannot override input_audio")
	}
	if value, ok := body["format"]; ok {
		f, valid := value.(string)
		if !valid || (f != "wav" && f != "mp3") || config != nil && config.Format != "" && config.Format != f {
			return nil, fmt.Errorf("invalid or conflicting ASR format")
		}
		format = f
		delete(body, "format")
	}
	if config != nil {
		if len(config.Language) == 1 {
			body["language"] = config.Language[0]
		} else if len(config.Language) > 1 {
			body["language"] = config.Language
		}
		if config.Prompt != "" {
			body["prompt"] = config.Prompt
		}
	}
	body["model"] = *name
	body["input_audio"] = map[string]any{"data": base64.StdEncoding.EncodeToString(audio), "format": format}
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(o.httpClient, o.BaseURL, o.URLSuffix.ASR, local, body, "")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	return readASRResponse(resp, local)
}

var _ ASRDriver = (*OpenRouterModel)(nil)
