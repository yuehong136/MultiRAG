package models

import "net/http"

func openAISpeechBody(name *string, text string, config *TTSConfig, stream bool) (map[string]any, string, error) {
	body, format, err := speechInput(name, text, config)
	if err != nil {
		return nil, "", err
	}
	delete(body, "format")
	delete(body, "audio_setting")
	body["model"], body["input"], body["response_format"], body["stream"] = *name, text, format, stream
	if config != nil && config.Voice != "" {
		body["voice"] = config.Voice
	}
	return body, format, nil
}

// openAIBinarySpeech shares bounded binary decoding and synchronous sender ownership.
func openAIBinarySpeech(client *http.Client, urls map[string]string, suffix string, name *string, text string, api *APIConfig, config *TTSConfig, send func(*TTSResponse) error) (*TTSResponse, error) {
	body, format, err := openAISpeechBody(name, text, config, send != nil)
	if err != nil {
		return nil, err
	}
	local, cancel := modelLongOpConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(client, urls, suffix, local, body, "")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if send != nil {
		return nil, streamSpeechBytes(resp, format, local, send)
	}
	result, err := readSpeechBytes(resp, format)
	if err != nil {
		return nil, err
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return result, nil
}
