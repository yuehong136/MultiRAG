package models

import "fmt"

func (c *CometAPIModel) TranscribeAudio(name *string, audio []byte, api *APIConfig, config *ASRConfig) (*ASRResponse, error) {
	return multipartASR(c.httpClient, c.BaseURL, c.URLSuffix.ASR, name, audio, api, config)
}

func (c *CometAPIModel) AudioSpeech(name *string, text string, api *APIConfig, config *TTSConfig) (*TTSResponse, error) {
	body, format, err := speechInput(name, text, config)
	if err != nil {
		return nil, err
	}
	delete(body, "format")
	delete(body, "audio_setting")
	body["model"], body["input"], body["response_format"] = *name, text, format
	if config != nil && config.Voice != "" {
		body["voice"] = config.Voice
	}
	if voice, exists := body["voice"]; exists {
		if value, ok := voice.(string); !ok || value == "" {
			return nil, fmt.Errorf("cometapi: voice must be a nonempty string")
		}
	}
	local, cancel := modelLongOpConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(c.httpClient, c.BaseURL, c.URLSuffix.TTS, local, body, "")
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

var _ ASRDriver = (*CometAPIModel)(nil)
var _ TTSDriver = (*CometAPIModel)(nil)
