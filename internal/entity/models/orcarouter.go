package models

import "time"

// OrcaRouterModel implements chat, discovery and binary speech.
type OrcaRouterModel struct{ *openAIChatDriver }

func NewOrcaRouterModel(urls map[string]string, suffix URLSuffix) *OrcaRouterModel {
	d := newGatewayChatDriver(urls, suffix, "orcarouter")
	d.buildBody = orcaRouterChatBody
	d.sseEvents = true
	d.streamIdleTimeout = 60 * time.Second
	d.httpClient.CheckRedirect = rejectModelRedirect
	return &OrcaRouterModel{d}
}
func (o *OrcaRouterModel) NewInstance(urls map[string]string) ModelDriver {
	return NewOrcaRouterModel(urls, o.URLSuffix)
}
func orcaRouterChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	body, err := validatedHostedChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	if config == nil || config.Temperature == nil {
		body["temperature"] = 1
	}
	if config != nil && config.Effort != nil {
		body["reasoning_effort"] = *config.Effort
	}
	return body, nil
}
func (o *OrcaRouterModel) AudioSpeech(name *string, text string, api *APIConfig, config *TTSConfig) (*TTSResponse, error) {
	body, format, err := speechInput(name, text, config)
	if err != nil {
		return nil, err
	}
	delete(body, "format")
	delete(body, "audio_setting")
	body["model"], body["input"], body["stream"], body["response_format"] = *name, text, false, format
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

var _ ModelDriver = (*OrcaRouterModel)(nil)
var _ TTSDriver = (*OrcaRouterModel)(nil)
