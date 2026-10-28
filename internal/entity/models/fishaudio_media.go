package models

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"mime/multipart"
	"strings"
)

func fishSpeechBody(name *string, text string, config *TTSConfig) (map[string]any, string, error) {
	body, format, err := speechInput(name, text, config)
	if err != nil {
		return nil, "", err
	}
	delete(body, "response_format")
	delete(body, "audio_setting")
	body["text"] = text
	body["format"] = format
	if config != nil && config.Voice != "" {
		body["reference_id"] = config.Voice
	}
	return body, format, nil
}
func (f *FishAudioModel) AudioSpeech(name *string, text string, api *APIConfig, config *TTSConfig) (*TTSResponse, error) {
	body, format, err := fishSpeechBody(name, text, config)
	if err != nil {
		return nil, err
	}
	local, cancel := modelLongOpConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(f.driver.httpClient, f.BaseURL, f.URLSuffix.TTS, local, body, *name)
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
func (f *FishAudioModel) AudioSpeechWithSender(name *string, text string, api *APIConfig, config *TTSConfig, send func(*TTSResponse) error) error {
	if send == nil {
		return fmt.Errorf("speech sender is required")
	}
	body, format, err := fishSpeechBody(name, text, config)
	if err != nil {
		return err
	}
	if f.URLSuffix.TTS == "" {
		return fmt.Errorf("fishaudio: speech endpoint is missing")
	}
	local, cancel := modelStreamConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(f.driver.httpClient, f.BaseURL, strings.TrimRight(f.URLSuffix.TTS, "/")+"/stream/with-timestamp", local, body, *name)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	total := 0
	err = scanMediaSSE(resp, func(kind string, raw []byte) error {
		if kind != "" && kind != "message" {
			return fmt.Errorf("fishaudio: unexpected speech event")
		}
		var event struct {
			Audio *string         `json:"audio_base64"`
			Error json.RawMessage `json:"error"`
			Code  *int            `json:"code"`
		}
		if err := json.Unmarshal(raw, &event); err != nil {
			return fmt.Errorf("fishaudio: invalid speech JSON")
		}
		if (len(event.Error) > 0 && string(event.Error) != "null") || (event.Code != nil && *event.Code != 0) {
			return fmt.Errorf("fishaudio: speech provider error")
		}
		if event.Audio == nil {
			return fmt.Errorf("fishaudio: missing speech audio")
		}
		audio, err := base64.StdEncoding.Strict().DecodeString(*event.Audio)
		if err != nil {
			return fmt.Errorf("fishaudio: invalid speech base64")
		}
		if len(audio) == 0 {
			return nil
		} // Timestamp-only snapshots can carry an empty audio chunk.
		if total+len(audio) > MaxMediaBytes {
			return fmt.Errorf("fishaudio: speech stream exceeds 20 MiB")
		}
		if err := requestContext(local).Err(); err != nil {
			return err
		}
		if err := send(&TTSResponse{Audio: audio, Format: format}); err != nil {
			return err
		}
		total += len(audio)
		return nil
	})
	if err != nil {
		return err
	}
	if err := requestContext(local).Err(); err != nil {
		return err
	}
	// This endpoint has no terminal marker; only a clean EOF after complete SSE frames is success.
	if total == 0 {
		return fmt.Errorf("fishaudio: empty speech stream")
	}
	return nil
}
func (f *FishAudioModel) TranscribeAudio(name *string, audio []byte, api *APIConfig, config *ASRConfig) (*ASRResponse, error) {
	if name == nil || strings.TrimSpace(*name) == "" {
		return nil, fmt.Errorf("model name is required")
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
	fields, err := CloneMediaParams(params)
	if err != nil {
		return nil, err
	}
	if config != nil {
		if len(config.Language) == 1 {
			fields["language"] = config.Language[0]
		} else if len(config.Language) > 1 {
			fields["language"] = config.Language
		}
		if config.Prompt != "" {
			fields["prompt"] = config.Prompt
		}
	}
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	part, err := writer.CreateFormFile("audio", "audio."+format)
	if err != nil {
		return nil, fmt.Errorf("create audio multipart: %w", err)
	}
	if _, err = part.Write(audio); err != nil {
		return nil, err
	}
	for key, value := range fields {
		text, ok := value.(string)
		if !ok {
			raw, err := json.Marshal(value)
			if err != nil {
				return nil, fmt.Errorf("invalid ASR field")
			}
			text = string(raw)
		}
		if err := writer.WriteField(key, text); err != nil {
			return nil, fmt.Errorf("invalid ASR field")
		}
	}
	if err := writer.Close(); err != nil {
		return nil, err
	}
	local, cancel := modelLongOpConfig(api)
	defer cancel()
	resp, err := mediaRequest(f.driver.httpClient, f.BaseURL, f.URLSuffix.ASR, local, writer.FormDataContentType(), &body, *name)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, err := readMediaBody(resp.Body, MaxMediaBytes+MaxMediaParamsBytes)
	if err != nil {
		return nil, err
	}
	var result struct {
		Text  *string         `json:"text"`
		Error json.RawMessage `json:"error"`
		Code  *int            `json:"code"`
	}
	if err := json.Unmarshal(raw, &result); err != nil {
		return nil, fmt.Errorf("fishaudio: invalid transcription JSON")
	}
	if (len(result.Error) > 0 && string(result.Error) != "null") || (result.Code != nil && *result.Code != 0) {
		return nil, fmt.Errorf("fishaudio: transcription provider error")
	}
	response := &ASRResponse{Text: result.Text}
	if err := ValidateASRResponse(response); err != nil {
		return nil, err
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return response, nil
}

var _ ASRDriver = (*FishAudioModel)(nil)
var _ TTSDriver = (*FishAudioModel)(nil)
var _ TTSStreamDriver = (*FishAudioModel)(nil)
