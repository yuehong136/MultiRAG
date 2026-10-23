package models

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
