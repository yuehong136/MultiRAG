package models

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net/http"
	"strings"
	"time"
	"unicode"
)

func xiaomiOption(value string) bool {
	return value != "" && len(value) <= 128 && strings.IndexFunc(value, unicode.IsControl) < 0
}

func xiaomiParams(params map[string]any) (map[string]any, error) {
	p, err := CloneMediaParams(params)
	if err != nil {
		return nil, err
	}
	if p == nil {
		p = map[string]any{}
	}
	for key := range p {
		switch strings.ToLower(key) {
		case "messages", "modalities", "audio", "asr_options":
			return nil, fmt.Errorf("xiaomi media params cannot override %s", key)
		}
	}
	return p, nil
}

func (x *XiaomiModel) asrBody(name *string, audio []byte, config *ASRConfig) (map[string]any, error) {
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
	format, language := "wav", "auto"
	if config != nil {
		params = config.Params
		if config.Format != "" {
			format = config.Format
		}
		if len(config.Language) > 1 {
			return nil, fmt.Errorf("xiaomi ASR accepts one language")
		}
		if len(config.Language) == 1 {
			language = config.Language[0]
		}
	}
	body, err := xiaomiParams(params)
	if err != nil {
		return nil, err
	}
	if v, ok := body["language"]; ok {
		var valid bool
		language, valid = v.(string)
		if !valid {
			return nil, fmt.Errorf("invalid ASR language")
		}
		delete(body, "language")
	}
	if !xiaomiOption(language) {
		return nil, fmt.Errorf("invalid ASR language")
	}
	mimeType := "audio/wav"
	if format == "mp3" {
		mimeType = "audio/mpeg"
	}
	for _, key := range []string{"mime", "mime_type"} {
		if v, ok := body[key]; ok {
			s, valid := v.(string)
			if !valid || (s != "audio/wav" && s != "audio/x-wav" && s != "audio/mpeg" && s != "audio/mp3") {
				return nil, fmt.Errorf("invalid ASR MIME")
			}
			mimeType = s
			delete(body, key)
		}
	}
	body["model"] = strings.TrimSpace(*name)
	body["messages"] = []any{map[string]any{"role": "user", "content": []any{map[string]any{"type": "input_audio", "input_audio": map[string]any{"data": "data:" + mimeType + ";base64," + base64.StdEncoding.EncodeToString(audio)}}}}}
	body["asr_options"] = map[string]any{"language": language}
	if config != nil && config.Prompt != "" {
		body["prompt"] = config.Prompt
	}
	return body, nil
}

func (x *XiaomiModel) ttsBody(name *string, text string, config *TTSConfig) (map[string]any, string, error) {
	if name == nil || strings.TrimSpace(*name) == "" {
		return nil, "", fmt.Errorf("model name is required")
	}
	if err := ValidateSpeechText(text); err != nil {
		return nil, "", err
	}
	if err := ValidateTTSConfig(config); err != nil {
		return nil, "", err
	}
	format, voice := "wav", "mimo_default"
	var params map[string]any
	if config != nil {
		params = config.Params
		if config.Format != "" {
			format = config.Format
		}
		if config.Voice != "" {
			voice = config.Voice
		}
	}
	body, err := xiaomiParams(params)
	if err != nil {
		return nil, "", err
	}
	for key, target := range map[string]*string{"format": &format, "voice": &voice} {
		if v, ok := body[key]; ok {
			s, valid := v.(string)
			if !valid {
				return nil, "", fmt.Errorf("invalid TTS %s", key)
			}
			*target = s
			delete(body, key)
		}
	}
	if !ValidAudioFormat(format) || !xiaomiOption(voice) {
		return nil, "", fmt.Errorf("invalid TTS format or voice")
	}
	body["model"], body["messages"], body["audio"] = strings.TrimSpace(*name), []any{map[string]any{"role": "assistant", "content": text}}, map[string]any{"format": format, "voice": voice}
	return body, format, nil
}

type xiaomiMediaMessage struct {
	Content *string `json:"content"`
	Audio   *struct {
		Data string `json:"data"`
	} `json:"audio"`
}
type xiaomiMediaResult struct {
	Choices []struct {
		Message      *xiaomiMediaMessage `json:"message"`
		Delta        *xiaomiMediaMessage `json:"delta"`
		FinishReason *string             `json:"finish_reason"`
	} `json:"choices"`
	Usage json.RawMessage `json:"usage"`
	Error json.RawMessage `json:"error"`
}

// mediaCall keeps streaming synchronous: callback failure immediately closes the
// response, and an unframed EOF cannot become successful audio/transcription.
func (x *XiaomiModel) mediaCall(body map[string]any, api *APIConfig, stream bool, consume func(*xiaomiMediaMessage) error) error {
	local, cancel := modelLongOpConfig(api)
	if stream {
		cancel()
		local, cancel = modelStreamConfig(api)
	}
	defer cancel()
	var active func()
	if stream {
		ctx, stop := context.WithCancel(requestContext(local))
		defer stop()
		copyConfig := *local
		copyConfig.Context = ctx
		local = &copyConfig
		timer := time.AfterFunc(x.streamIdleTimeout, stop)
		defer timer.Stop()
		active = func() { timer.Reset(x.streamIdleTimeout) }
	}
	body["stream"] = stream
	resp, err := x.request(http.MethodPost, x.URLSuffix.Chat, local, body)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if active != nil {
		resp.Body = &activeModelStream{ReadCloser: resp.Body, active: active}
	}
	deliver := func(raw []byte, delta bool) (bool, error) {
		var result xiaomiMediaResult
		if err := json.Unmarshal(raw, &result); err != nil {
			return false, fmt.Errorf("invalid Xiaomi media JSON")
		}
		if len(result.Error) > 0 && string(result.Error) != "null" {
			return false, fmt.Errorf("Xiaomi media API error")
		}
		if len(result.Choices) == 0 {
			if delta && len(result.Usage) > 0 && string(result.Usage) != "null" {
				return false, nil
			}
			return false, fmt.Errorf("missing Xiaomi media choices")
		}
		if len(result.Choices) != 1 {
			return false, fmt.Errorf("ambiguous Xiaomi media choices")
		}
		choice := result.Choices[0]
		message := choice.Message
		if delta {
			message = choice.Delta
		}
		if message == nil {
			return false, fmt.Errorf("missing Xiaomi media message")
		}
		if err := requestContext(local).Err(); err != nil {
			return false, err
		}
		if err := consume(message); err != nil {
			return false, err
		}
		return choice.FinishReason != nil && *choice.FinishReason != "", nil
	}
	if !stream || !strings.HasPrefix(resp.Header.Get("Content-Type"), "text/event-stream") {
		typ, _, parseErr := mime.ParseMediaType(resp.Header.Get("Content-Type"))
		if parseErr != nil || typ != "application/json" {
			return fmt.Errorf("Xiaomi media requires JSON or SSE")
		}
		raw, err := readMediaBody(resp.Body, 2*MaxMediaBytes)
		if err != nil {
			return err
		}
		_, err = deliver(raw, false)
		return err
	}
	done := false
	stop := errors.New("xiaomi stream done")
	err = scanMediaSSE(resp, func(event string, raw []byte) error {
		if event == "error" {
			return fmt.Errorf("Xiaomi media SSE error")
		}
		if string(raw) == "[DONE]" {
			done = true
			return stop
		}
		active()
		_, err := deliver(raw, true)
		return err
	})
	if err != nil && !errors.Is(err, stop) {
		return err
	}
	if !done {
		return fmt.Errorf("incomplete Xiaomi media stream: %w", io.ErrUnexpectedEOF)
	}
	return requestContext(local).Err()
}

func (x *XiaomiModel) TranscribeAudio(name *string, audio []byte, api *APIConfig, config *ASRConfig) (*ASRResponse, error) {
	body, err := x.asrBody(name, audio, config)
	if err != nil {
		return nil, err
	}
	var text *string
	err = x.mediaCall(body, api, false, func(m *xiaomiMediaMessage) error {
		text = m.Content
		if text == nil {
			return fmt.Errorf("missing transcription")
		}
		return ValidateASRResponse(&ASRResponse{Text: text})
	})
	if err != nil {
		return nil, err
	}
	return &ASRResponse{Text: text}, nil
}
func (x *XiaomiModel) TranscribeAudioWithSender(name *string, audio []byte, api *APIConfig, config *ASRConfig, send func(*ASRResponse) error) error {
	if send == nil {
		return fmt.Errorf("sender is required")
	}
	body, err := x.asrBody(name, audio, config)
	if err != nil {
		return err
	}
	total, seen := 0, false
	err = x.mediaCall(body, api, true, func(m *xiaomiMediaMessage) error {
		if m.Content == nil {
			return nil
		}
		seen = true
		total += len(*m.Content)
		if total > MaxMediaBytes {
			return fmt.Errorf("transcription exceeds limit")
		}
		return send(&ASRResponse{Text: m.Content})
	})
	if err != nil {
		return err
	}
	if !seen {
		return fmt.Errorf("missing transcription")
	}
	return nil
}
func (x *XiaomiModel) AudioSpeech(name *string, text string, api *APIConfig, config *TTSConfig) (*TTSResponse, error) {
	body, format, err := x.ttsBody(name, text, config)
	if err != nil {
		return nil, err
	}
	var audio []byte
	err = x.mediaCall(body, api, false, func(m *xiaomiMediaMessage) error {
		if m.Audio == nil {
			return fmt.Errorf("missing speech audio")
		}
		var err error
		audio, err = DecodeMediaData(m.Audio.Data)
		return err
	})
	if err != nil {
		return nil, err
	}
	return &TTSResponse{Audio: audio, Format: format}, nil
}
func (x *XiaomiModel) AudioSpeechWithSender(name *string, text string, api *APIConfig, config *TTSConfig, send func(*TTSResponse) error) error {
	if send == nil {
		return fmt.Errorf("sender is required")
	}
	body, format, err := x.ttsBody(name, text, config)
	if err != nil {
		return err
	}
	total := 0
	err = x.mediaCall(body, api, true, func(m *xiaomiMediaMessage) error {
		if m.Audio == nil || m.Audio.Data == "" {
			return nil
		}
		audio, err := DecodeMediaData(m.Audio.Data)
		if err != nil {
			return err
		}
		total += len(audio)
		if total > MaxMediaBytes {
			return fmt.Errorf("speech stream exceeds limit")
		}
		return send(&TTSResponse{Audio: audio, Format: format})
	})
	if err != nil {
		return err
	}
	if total == 0 {
		return fmt.Errorf("missing speech audio")
	}
	return nil
}

var _ ASRDriver = (*XiaomiModel)(nil)
var _ ASRStreamDriver = (*XiaomiModel)(nil)
var _ TTSDriver = (*XiaomiModel)(nil)
var _ TTSStreamDriver = (*XiaomiModel)(nil)
