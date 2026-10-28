package models

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
)

type minimaxSpeechEvent struct {
	BaseResp *struct {
		Code *int `json:"status_code"`
	} `json:"base_resp"`
	Data *struct {
		Audio  string `json:"audio"`
		Status *int   `json:"status"`
	} `json:"data"`
	Extra *struct {
		Format string `json:"audio_format"`
	} `json:"extra_info"`
}

func decodeMinimaxSpeech(raw []byte, format string) (*minimaxSpeechEvent, error) {
	var event minimaxSpeechEvent
	if err := json.Unmarshal(raw, &event); err != nil {
		return nil, fmt.Errorf("minimax: invalid speech JSON")
	}
	if event.BaseResp == nil || event.BaseResp.Code == nil {
		return nil, fmt.Errorf("minimax: missing speech business code")
	}
	if *event.BaseResp.Code != 0 {
		return nil, fmt.Errorf("minimax: speech error (%d)", *event.BaseResp.Code)
	}
	if event.Data == nil || event.Data.Status == nil || (*event.Data.Status != 1 && *event.Data.Status != 2) {
		return nil, fmt.Errorf("minimax: missing or invalid speech status")
	}
	if event.Extra != nil && event.Extra.Format != "" && event.Extra.Format != format {
		return nil, fmt.Errorf("minimax: speech format mismatch")
	}
	return &event, nil
}
func minimaxSpeechBody(name *string, text string, config *TTSConfig, stream bool) (map[string]any, string, error) {
	body, format, err := speechInput(name, text, config)
	if err != nil {
		return nil, "", err
	}
	setting := map[string]any{}
	if v, ok := body["audio_setting"]; ok {
		var valid bool
		setting, valid = v.(map[string]any)
		if !valid {
			return nil, "", fmt.Errorf("minimax: audio_setting must be an object")
		}
	}
	setting["format"] = format
	body["audio_setting"] = setting
	if config != nil && config.Voice != "" {
		voice := map[string]any{}
		if v, ok := body["voice_setting"]; ok {
			var valid bool
			voice, valid = v.(map[string]any)
			if !valid {
				return nil, "", fmt.Errorf("minimax: voice_setting must be an object")
			}
		}
		voice["voice_id"] = config.Voice
		body["voice_setting"] = voice
	}
	if v, ok := body["output_format"]; ok && v != "hex" {
		return nil, "", fmt.Errorf("minimax: output_format must be hex")
	}
	delete(body, "format")
	delete(body, "response_format")
	body["model"] = *name
	body["text"] = text
	body["stream"] = stream
	body["output_format"] = "hex"
	if stream {
		options := map[string]any{}
		if v, ok := body["stream_options"]; ok {
			var valid bool
			options, valid = v.(map[string]any)
			if !valid {
				return nil, "", fmt.Errorf("minimax: stream_options must be an object")
			}
		}
		// The last event otherwise repeats the complete audio, duplicating streamed chunks.
		options["exclude_aggregated_audio"] = true
		body["stream_options"] = options
	}
	return body, format, nil
}
func (m *MinimaxModel) AudioSpeech(name *string, text string, api *APIConfig, config *TTSConfig) (*TTSResponse, error) {
	body, format, err := minimaxSpeechBody(name, text, config, false)
	if err != nil {
		return nil, err
	}
	local, cancel := modelLongOpConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(m.httpClient, m.BaseURL, m.URLSuffix.TTS, local, body, "")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, err := readMediaBody(resp.Body, 2*MaxMediaBytes+MaxMediaParamsBytes)
	if err != nil {
		return nil, err
	}
	event, err := decodeMinimaxSpeech(raw, format)
	if err != nil {
		return nil, err
	}
	if *event.Data.Status != 2 {
		return nil, fmt.Errorf("minimax: speech not complete")
	}
	audio, err := hex.DecodeString(event.Data.Audio)
	if err != nil {
		return nil, fmt.Errorf("minimax: invalid speech hex")
	}
	result := &TTSResponse{Audio: audio, Format: format}
	if err := ValidateTTSResponse(result); err != nil {
		return nil, err
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return result, nil
}
func (m *MinimaxModel) AudioSpeechWithSender(name *string, text string, api *APIConfig, config *TTSConfig, send func(*TTSResponse) error) error {
	if send == nil {
		return fmt.Errorf("speech sender is required")
	}
	body, format, err := minimaxSpeechBody(name, text, config, true)
	if err != nil {
		return err
	}
	local, cancel := modelStreamConfig(api)
	defer cancel()
	resp, err := mediaJSONRequest(m.httpClient, m.BaseURL, m.URLSuffix.TTS, local, body, "")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	done, total := false, 0
	digest := sha256.New()
	err = scanMediaSSE(resp, func(kind string, raw []byte) error {
		if done {
			return fmt.Errorf("minimax: speech event after completion")
		}
		if kind != "" && kind != "message" {
			return fmt.Errorf("minimax: unexpected speech event")
		}
		event, err := decodeMinimaxSpeech(raw, format)
		if err != nil {
			return err
		}
		audio, err := hex.DecodeString(event.Data.Audio)
		if err != nil {
			return fmt.Errorf("minimax: invalid speech hex")
		}
		if *event.Data.Status == 2 {
			done = true
			// Some servers ignore exclude_aggregated_audio; validate and discard the duplicate.
			if total > 0 && len(audio) > 0 {
				sum := sha256.Sum256(audio)
				if len(audio) != total || !bytes.Equal(sum[:], digest.Sum(nil)) {
					return fmt.Errorf("minimax: inconsistent final speech aggregate")
				}
				audio = nil
			}
		}
		if len(audio) > 0 {
			if total+len(audio) > MaxMediaBytes {
				return fmt.Errorf("minimax: speech stream exceeds 20 MiB")
			}
			if err := requestContext(local).Err(); err != nil {
				return err
			}
			if err := send(&TTSResponse{Audio: audio, Format: format}); err != nil {
				return err
			}
			total += len(audio)
			digest.Write(audio)
		}
		return nil
	})
	if err != nil {
		return err
	}
	if err := requestContext(local).Err(); err != nil {
		return err
	}
	if !done {
		return fmt.Errorf("minimax: speech stream missing final status: %w", io.ErrUnexpectedEOF)
	}
	if total == 0 {
		return fmt.Errorf("minimax: empty speech stream")
	}
	return nil
}

var _ TTSDriver = (*MinimaxModel)(nil)
var _ TTSStreamDriver = (*MinimaxModel)(nil)
