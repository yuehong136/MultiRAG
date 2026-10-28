package models

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
)

func stepFunSpeechBody(name *string, text string, config *TTSConfig, stream bool) (map[string]any, string, error) {
	body, format, err := speechInput(name, text, config)
	if err != nil {
		return nil, "", err
	}
	if _, ok := body["stream_format"]; ok {
		return nil, "", fmt.Errorf("stream_format is selected by the speech operation")
	}
	delete(body, "format")
	delete(body, "audio_setting")
	body["model"], body["input"], body["response_format"] = *name, text, format
	if stream {
		body["stream_format"] = "sse"
	}
	if config != nil && config.Voice != "" {
		body["voice"] = config.Voice
	}
	return body, format, nil
}

func (s *StepFunModel) AudioSpeech(name *string, text string, api *APIConfig, config *TTSConfig) (*TTSResponse, error) {
	body, format, err := stepFunSpeechBody(name, text, config, false)
	if err != nil {
		return nil, err
	}
	local, cancel := modelLongOpConfig(api)
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

func (s *StepFunModel) AudioSpeechWithSender(name *string, text string, api *APIConfig, config *TTSConfig, send func(*TTSResponse) error) error {
	if send == nil {
		return fmt.Errorf("speech sender required")
	}
	body, format, err := stepFunSpeechBody(name, text, config, true)
	if err != nil {
		return err
	}
	local, cancel := modelStreamConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(s.httpClient, s.BaseURL, s.URLSuffix.TTS, local, body, "")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	done, total := false, 0
	err = scanMediaSSE(resp, func(event string, raw []byte) error {
		if event == "error" || event == "speech.audio.error" {
			return fmt.Errorf("StepFun speech provider error")
		}
		if string(raw) == "[DONE]" {
			done = true
			return nil
		}
		var frame struct {
			Type  string          `json:"type"`
			Audio *string         `json:"audio"`
			Error json.RawMessage `json:"error"`
		}
		if err := json.Unmarshal(raw, &frame); err != nil {
			return fmt.Errorf("invalid StepFun speech JSON")
		}
		if len(frame.Error) > 0 && string(frame.Error) != "null" || frame.Type == "speech.audio.error" || frame.Type == "error" {
			return fmt.Errorf("StepFun speech provider error")
		}
		switch frame.Type {
		case "speech.audio.done":
			done = true
			return nil
		case "speech.audio.delta":
			if done {
				return fmt.Errorf("StepFun audio after completion")
			}
			if frame.Audio == nil {
				return fmt.Errorf("missing StepFun audio")
			}
			audio, err := base64.StdEncoding.Strict().DecodeString(*frame.Audio)
			if err != nil || len(audio) == 0 {
				return fmt.Errorf("invalid StepFun speech base64")
			}
			if total+len(audio) > MaxMediaBytes {
				return fmt.Errorf("speech stream exceeds limit")
			}
			if err := requestContext(local).Err(); err != nil {
				return err
			}
			if err := send(&TTSResponse{Audio: audio, Format: format}); err != nil {
				return err
			}
			total += len(audio)
		default:
			return fmt.Errorf("unexpected StepFun speech event")
		}
		return nil
	})
	if err != nil {
		return err
	}
	if err := requestContext(local).Err(); err != nil {
		return err
	}
	if !done || total == 0 {
		return fmt.Errorf("incomplete StepFun speech stream: %w", io.ErrUnexpectedEOF)
	}
	return nil
}

var _ TTSDriver = (*StepFunModel)(nil)
var _ TTSStreamDriver = (*StepFunModel)(nil)
