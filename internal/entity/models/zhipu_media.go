package models

import (
	"fmt"
	"net/http"
)

// Media calls use their own context deadline, retaining the configured transport
// without inheriting the legacy client's whole-response timeout for speech streams.
func (z *ZhipuAIModel) mediaClient() *http.Client {
	client := *z.httpClient
	client.Timeout = 0
	return &client
}

func (z *ZhipuAIModel) TranscribeAudio(name *string, audio []byte, api *APIConfig, config *ASRConfig) (*ASRResponse, error) {
	return multipartASRForm(z.mediaClient(), z.BaseURL, z.URLSuffix.ASR, name, audio, api, config, true)
}

func (z *ZhipuAIModel) AudioSpeech(name *string, text string, api *APIConfig, config *TTSConfig) (*TTSResponse, error) {
	return openAIBinarySpeech(z.mediaClient(), z.BaseURL, z.URLSuffix.TTS, name, text, api, config, nil)
}

func (z *ZhipuAIModel) AudioSpeechWithSender(name *string, text string, api *APIConfig, config *TTSConfig, send func(*TTSResponse) error) error {
	if send == nil {
		return fmt.Errorf("speech sender required")
	}
	_, err := openAIBinarySpeech(z.mediaClient(), z.BaseURL, z.URLSuffix.TTS, name, text, api, config, send)
	return err
}

var _ ASRDriver = (*ZhipuAIModel)(nil)
var _ TTSDriver = (*ZhipuAIModel)(nil)
var _ TTSStreamDriver = (*ZhipuAIModel)(nil)
