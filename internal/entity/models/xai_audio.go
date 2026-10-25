package models

import (
	"fmt"
	"strings"
)

// TranscribeAudio sends authorized bytes with the xAI multipart STT protocol.
func (x *XAIModel) TranscribeAudio(name *string, audio []byte, api *APIConfig, config *ASRConfig) (*ASRResponse, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, format, err := openAIASRRequest(x.httpClient, x.BaseURL, x.URLSuffix.ASR, name, audio, local, config, false)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	return decodeAudioTranscription(resp, format, local)
}

func xaiSpeechBody(name *string, text string, config *TTSConfig) (map[string]any, string, error) {
	body, format, err := speechInput(name, text, config)
	if err != nil {
		return nil, "", err
	}
	// The model selected for xAI speech is its voice identifier. Callers cannot
	// replace trusted input or voice through dynamic provider parameters.
	for _, key := range []string{"text", "voice_id", "output_format", "model", "input", "stream", "stream_format"} {
		if _, exists := body[key]; exists {
			return nil, "", fmt.Errorf("xai: reserved speech parameter %s", key)
		}
	}
	voice := *name
	if config != nil && config.Voice != "" {
		voice = config.Voice
	}
	body["text"], body["voice_id"] = text, voice
	body["output_format"] = map[string]any{"codec": format}
	return body, format, nil
}

func (x *XAIModel) AudioSpeech(name *string, text string, api *APIConfig, config *TTSConfig) (*TTSResponse, error) {
	body, format, err := xaiSpeechBody(name, text, config)
	if err != nil {
		return nil, err
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(x.httpClient, x.BaseURL, x.URLSuffix.TTS, local, body, "")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	result, err := readSpeechBytes(resp, format)
	if err == nil {
		err = requestContext(local).Err()
	}
	if err != nil {
		return nil, err
	}
	return result, nil
}

// xAI TTS returns binary audio. SSE is rejected by MIME validation; no OpenAI
// event schema is assumed for a provider that does not expose that protocol.
func (x *XAIModel) AudioSpeechWithSender(name *string, text string, api *APIConfig, config *TTSConfig, send func(*TTSResponse) error) error {
	if send == nil {
		return fmt.Errorf("xai: speech sender required")
	}
	body, format, err := xaiSpeechBody(name, text, config)
	if err != nil {
		return err
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(x.httpClient, x.BaseURL, x.URLSuffix.TTS, local, body, "")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream") {
		return fmt.Errorf("%w: xai speech SSE", ErrUnsupportedCapability)
	}
	return streamSpeechBytes(resp, format, local, send)
}

var _ ASRDriver = (*XAIModel)(nil)
var _ TTSDriver = (*XAIModel)(nil)
var _ TTSStreamDriver = (*XAIModel)(nil)
