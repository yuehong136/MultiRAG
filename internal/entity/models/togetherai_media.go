package models

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

func (t *TogetherAIModel) Rerank(name *string, query string, documents []string, api *APIConfig, config *RerankConfig) (*RerankResponse, error) {
	return rerankHTTP(t.httpClient, t.BaseURL, t.URLSuffix.Rerank, name, query, documents, api, config, nil)
}

func (t *TogetherAIModel) TranscribeAudio(name *string, audio []byte, api *APIConfig, config *ASRConfig) (*ASRResponse, error) {
	return multipartASR(t.httpClient, t.BaseURL, t.URLSuffix.ASR, name, audio, api, config)
}

func (t *TogetherAIModel) AudioSpeech(name *string, text string, api *APIConfig, config *TTSConfig) (*TTSResponse, error) {
	return openAIBinarySpeech(t.httpClient, t.BaseURL, t.URLSuffix.TTS, name, text, api, config, nil)
}

func (t *TogetherAIModel) AudioSpeechWithSender(name *string, text string, api *APIConfig, config *TTSConfig, send func(*TTSResponse) error) error {
	if send == nil {
		return fmt.Errorf("speech sender required")
	}
	body, format, err := openAISpeechBody(name, text, config, true)
	if err != nil {
		return err
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(t.httpClient, t.BaseURL, t.URLSuffix.TTS, local, body, "")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	total := 0
	done := errors.New("speech stream completed")
	err = scanMediaSSE(resp, func(eventName string, raw []byte) error {
		if err := requestContext(local).Err(); err != nil {
			return err
		}
		if string(raw) == "[DONE]" {
			if total == 0 {
				return fmt.Errorf("empty speech stream")
			}
			return done
		}
		var event struct {
			Type  string          `json:"type"`
			Delta *string         `json:"delta"`
			Error json.RawMessage `json:"error"`
		}
		if err := json.Unmarshal(raw, &event); err != nil {
			return fmt.Errorf("invalid TogetherAI speech event")
		}
		if eventName == "error" || len(event.Error) > 0 && string(event.Error) != "null" || event.Type == "error" {
			return fmt.Errorf("TogetherAI speech provider error")
		}
		switch event.Type {
		case "conversation.item.audio_output.delta":
			if event.Delta == nil {
				return fmt.Errorf("missing speech delta")
			}
			audio, err := base64.StdEncoding.Strict().DecodeString(*event.Delta)
			if err != nil || len(audio) == 0 || total+len(audio) > MaxMediaBytes {
				return fmt.Errorf("invalid or oversized speech delta")
			}
			if err := send(&TTSResponse{Audio: audio, Format: format}); err != nil {
				return err
			}
			total += len(audio)
			return requestContext(local).Err()
		case "conversation.item.audio_output.done":
			return nil
		default:
			return fmt.Errorf("unknown TogetherAI speech event")
		}
	})
	if errors.Is(err, done) {
		return requestContext(local).Err()
	}
	if err != nil {
		return err
	}
	return fmt.Errorf("TogetherAI speech stream ended before completion: %w", io.ErrUnexpectedEOF)
}

var _ ASRDriver = (*TogetherAIModel)(nil)
var _ TTSDriver = (*TogetherAIModel)(nil)
var _ TTSStreamDriver = (*TogetherAIModel)(nil)
