package models

import "fmt"

func (x *XinferenceModel) TranscribeAudio(name *string, audio []byte, api *APIConfig, config *ASRConfig) (*ASRResponse, error) {
	return multipartASR(x.httpClient, x.BaseURL, x.URLSuffix.ASR, name, audio, api, config, mediaHTTPOptions{optionalAPIKey: true})
}

func (x *XinferenceModel) AudioSpeech(name *string, text string, api *APIConfig, config *TTSConfig) (*TTSResponse, error) {
	body, format, err := speechInput(name, text, config)
	if err != nil {
		return nil, err
	}
	delete(body, "format")
	delete(body, "audio_setting")
	body["model"], body["input"], body["response_format"], body["stream"] = *name, text, format, false
	if config != nil && config.Voice != "" {
		body["voice"] = config.Voice
	}
	if voice, exists := body["voice"]; exists {
		if _, ok := voice.(string); !ok {
			return nil, fmt.Errorf("xinference: voice must be a string")
		}
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(x.httpClient, x.BaseURL, x.URLSuffix.TTS, local, body, "", mediaHTTPOptions{optionalAPIKey: true})
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

var _ ASRDriver = (*XinferenceModel)(nil)
var _ TTSDriver = (*XinferenceModel)(nil)
