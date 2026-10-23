package models

import "fmt"

func (s *SiliconFlowModel) TranscribeAudio(name *string, audio []byte, api *APIConfig, config *ASRConfig) (*ASRResponse, error) {
	return multipartASR(s.httpClient, s.BaseURL, s.URLSuffix.ASR, name, audio, api, config)
}

func siliconFlowSpeechBody(name *string, text string, config *TTSConfig, stream bool) (map[string]any, string, error) {
	body, format, err := speechInput(name, text, config)
	if err != nil {
		return nil, "", err
	}
	defaults := map[string]any{"voice": *name + ":anna", "sample_rate": 32000, "speed": 1, "gain": 0}
	for key, value := range defaults {
		if _, ok := body[key]; !ok {
			body[key] = value
		}
	}
	delete(body, "format")
	delete(body, "audio_setting")
	body["model"], body["input"], body["stream"], body["response_format"] = *name, text, stream, format
	if config != nil && config.Voice != "" {
		body["voice"] = config.Voice
	}
	if voice, ok := body["voice"].(string); !ok || voice == "" {
		return nil, "", fmt.Errorf("SiliconFlow voice required")
	}
	return body, format, nil
}

func (s *SiliconFlowModel) AudioSpeech(name *string, text string, api *APIConfig, config *TTSConfig) (*TTSResponse, error) {
	body, format, err := siliconFlowSpeechBody(name, text, config, false)
	if err != nil {
		return nil, err
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(s.httpClient, s.BaseURL, s.URLSuffix.TTS, local, body, "")
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

func (s *SiliconFlowModel) AudioSpeechWithSender(name *string, text string, api *APIConfig, config *TTSConfig, send func(*TTSResponse) error) error {
	if send == nil {
		return fmt.Errorf("speech sender required")
	}
	body, format, err := siliconFlowSpeechBody(name, text, config, true)
	if err != nil {
		return err
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(s.httpClient, s.BaseURL, s.URLSuffix.TTS, local, body, "")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	return streamSpeechBytes(resp, format, local, send)
}

var _ ASRDriver = (*SiliconFlowModel)(nil)
var _ TTSDriver = (*SiliconFlowModel)(nil)
var _ TTSStreamDriver = (*SiliconFlowModel)(nil)
