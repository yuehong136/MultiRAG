package models

import (
	"fmt"
	"strings"
)

func (d *DeepInfraModel) TranscribeAudio(name *string, audio []byte, api *APIConfig, config *ASRConfig) (*ASRResponse, error) {
	return multipartASR(d.httpClient, d.BaseURL, d.URLSuffix.ASR, name, audio, api, config)
}

func deepInfraSpeechBody(name *string, text string, config *TTSConfig) (map[string]any, string, string, error) {
	body, format, err := speechInput(name, text, config)
	if err != nil {
		return nil, "", "", err
	}
	voice := ""
	for _, field := range []string{"voice", "voice_id"} {
		if value, exists := body[field]; exists {
			next, ok := value.(string)
			if !ok || strings.TrimSpace(next) == "" {
				return nil, "", "", fmt.Errorf("deepinfra: invalid voice_id")
			}
			voice = next
		}
	}
	if config != nil && config.Voice != "" {
		voice = config.Voice
	}
	if output, exists := body["output_format"]; exists {
		value, ok := output.(string)
		if !ok || !ValidAudioFormat(value) {
			return nil, "", "", fmt.Errorf("deepinfra: invalid output_format")
		}
		if config == nil || config.Format == "" {
			format = value
		}
	}
	if voice == "" || len(voice) > 128 || strings.IndexFunc(voice, func(r rune) bool {
		return !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '_' || r == '-')
	}) >= 0 {
		return nil, "", "", fmt.Errorf("deepinfra: voice_id must be a nonempty identifier")
	}
	if format == "aac" {
		return nil, "", "", fmt.Errorf("deepinfra: unsupported speech format")
	}
	delete(body, "voice")
	delete(body, "voice_id")
	delete(body, "format")
	delete(body, "response_format")
	delete(body, "audio_setting")
	body["text"], body["output_format"], body["model_id"] = text, format, *name
	return body, voice, format, nil
}

func (d *DeepInfraModel) AudioSpeech(name *string, text string, api *APIConfig, config *TTSConfig) (*TTSResponse, error) {
	body, voice, format, err := deepInfraSpeechBody(name, text, config)
	if err != nil {
		return nil, err
	}
	if d.URLSuffix.TTS == "" {
		return nil, fmt.Errorf("deepinfra: speech endpoint is missing")
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(d.httpClient, d.BaseURL, strings.TrimRight(d.URLSuffix.TTS, "/")+"/"+voice, local, body, "")
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

func (d *DeepInfraModel) AudioSpeechWithSender(name *string, text string, api *APIConfig, config *TTSConfig, send func(*TTSResponse) error) error {
	if send == nil {
		return fmt.Errorf("deepinfra: speech sender is required")
	}
	body, voice, format, err := deepInfraSpeechBody(name, text, config)
	if err != nil {
		return err
	}
	if d.URLSuffix.TTS == "" {
		return fmt.Errorf("deepinfra: speech endpoint is missing")
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(d.httpClient, d.BaseURL, strings.TrimRight(d.URLSuffix.TTS, "/")+"/"+voice+"/stream", local, body, "")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	return streamSpeechBytes(resp, format, local, send)
}

var _ ASRDriver = (*DeepInfraModel)(nil)
var _ TTSDriver = (*DeepInfraModel)(nil)
var _ TTSStreamDriver = (*DeepInfraModel)(nil)
