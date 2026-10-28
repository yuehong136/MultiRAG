package models

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"mime/multipart"
	"net/http"
	"sort"
	"strings"
)

var errAudioTerminal = errors.New("audio terminal received")

// openAIASRRequest accepts authorized bytes rather than an arbitrary local path.
func openAIASRRequest(client *http.Client, urls map[string]string, suffix string, name *string, audio []byte, api *APIConfig, config *ASRConfig, stream bool) (*http.Response, string, error) {
	if name == nil || strings.TrimSpace(*name) == "" {
		return nil, "", fmt.Errorf("ASR model required")
	}
	if err := ValidateMediaBytes(audio); err != nil {
		return nil, "", err
	}
	if err := ValidateASRConfig(config); err != nil {
		return nil, "", err
	}
	params := map[string]any{}
	format := "wav"
	if config != nil {
		var err error
		params, err = CloneMediaParams(config.Params)
		if err != nil {
			return nil, "", err
		}
		if config.Format != "" {
			format = config.Format
		}
		if len(config.Language) > 1 {
			return nil, "", fmt.Errorf("ASR requires a single language")
		}
		if len(config.Language) == 1 {
			params["language"] = config.Language[0]
		}
		if config.Prompt != "" {
			params["prompt"] = config.Prompt
		}
	}
	responseFormat := "json"
	if value, exists := params["response_format"]; exists {
		next, ok := value.(string)
		if !ok {
			return nil, "", fmt.Errorf("invalid ASR response_format")
		}
		switch next {
		case "json", "verbose_json", "diarized_json", "text", "srt", "vtt":
			responseFormat = next
		default:
			return nil, "", fmt.Errorf("invalid ASR response_format")
		}
	}
	if stream && responseFormat != "json" && responseFormat != "diarized_json" {
		return nil, "", fmt.Errorf("ASR streaming requires JSON response_format")
	}
	if value, exists := params["temperature"]; exists {
		number, ok := value.(float64)
		if !ok || number < 0 || number > 1 {
			return nil, "", fmt.Errorf("invalid ASR temperature")
		}
	}
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	if err := writer.WriteField("model", *name); err != nil {
		return nil, "", err
	}
	keys := make([]string, 0, len(params))
	for key := range params {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		if err := writeMultipartASRField(writer, key, params[key], true); err != nil {
			return nil, "", err
		}
	}
	if stream {
		if err := writer.WriteField("stream", "true"); err != nil {
			return nil, "", err
		}
	}
	part, err := writer.CreateFormFile("file", "audio."+format)
	if err != nil {
		return nil, "", err
	}
	if _, err = part.Write(audio); err != nil {
		return nil, "", err
	}
	if err = writer.Close(); err != nil {
		return nil, "", err
	}
	accept := "application/json"
	if stream {
		accept = "text/event-stream"
	}
	resp, err := mediaRequest(client, urls, suffix, api, writer.FormDataContentType(), &body, "", mediaHTTPOptions{accept: accept})
	return resp, responseFormat, err
}

func decodeAudioTranscription(resp *http.Response, format string, api *APIConfig) (*ASRResponse, error) {
	if format != "text" && format != "srt" && format != "vtt" {
		return readASRResponse(resp, api)
	}
	raw, err := readMediaBody(resp.Body, MaxMediaBytes)
	if err != nil {
		return nil, err
	}
	if len(raw) == 0 {
		return nil, fmt.Errorf("empty transcription response")
	}
	// An HTTP-200 JSON error is never a text transcription.
	var envelope struct {
		Error json.RawMessage `json:"error"`
	}
	jsonError := json.Unmarshal(raw, &envelope) == nil && len(envelope.Error) > 0 && string(envelope.Error) != "null"
	if strings.Contains(resp.Header.Get("Content-Type"), "json") || jsonError {
		return nil, fmt.Errorf("provider returned JSON for text transcription")
	}
	text := string(raw)
	if err := requestContext(api).Err(); err != nil {
		return nil, err
	}
	return &ASRResponse{Text: &text}, nil
}

func (o *OpenAIModel) TranscribeAudio(name *string, audio []byte, api *APIConfig, config *ASRConfig) (*ASRResponse, error) {
	local, cancel := modelLongOpConfig(api)
	defer cancel()
	resp, format, err := openAIASRRequest(o.httpClient, o.BaseURL, o.URLSuffix.ASR, name, audio, local, config, false)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	return decodeAudioTranscription(resp, format, local)
}

func (o *OpenAIModel) TranscribeAudioWithSender(name *string, audio []byte, api *APIConfig, config *ASRConfig, send func(*ASRResponse) error) error {
	if send == nil {
		return fmt.Errorf("ASR sender required")
	}
	local, cancel := modelStreamConfig(api)
	defer cancel()
	resp, format, err := openAIASRRequest(o.httpClient, o.BaseURL, o.URLSuffix.ASR, name, audio, local, config, true)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if !strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream") {
		result, err := decodeAudioTranscription(resp, format, local)
		if err != nil {
			return err
		}
		if err = send(result); err != nil {
			return err
		}
		return requestContext(local).Err()
	}
	received, terminal := false, false
	total := 0
	err = scanMediaSSE(resp, func(_ string, raw []byte) error {
		if err := requestContext(local).Err(); err != nil {
			return err
		}
		if string(raw) == "[DONE]" {
			terminal = true
			return errAudioTerminal
		}
		var event struct {
			Type  string          `json:"type"`
			Delta *string         `json:"delta"`
			Text  *string         `json:"text"`
			Error json.RawMessage `json:"error"`
		}
		if err := json.Unmarshal(raw, &event); err != nil {
			return fmt.Errorf("invalid transcription SSE")
		}
		if len(event.Error) > 0 && string(event.Error) != "null" || event.Type == "error" {
			return fmt.Errorf("transcription provider error")
		}
		var text *string
		switch event.Type {
		case "transcript.text.delta":
			text = event.Delta
		case "transcript.text.segment":
			text = event.Text
		case "transcript.text.done":
			terminal = true
			if !received {
				text = event.Text
			}
		default:
			return fmt.Errorf("unknown transcription SSE event")
		}
		if text == nil && !terminal {
			return fmt.Errorf("missing transcription SSE text")
		}
		if text != nil {
			total += len(*text)
			if total > MaxMediaBytes {
				return fmt.Errorf("transcription stream exceeds limit")
			}
			result := &ASRResponse{Text: text}
			if err := ValidateASRResponse(result); err != nil {
				return err
			}
			if err := send(result); err != nil {
				return err
			}
			received = true
		}
		if terminal {
			return errAudioTerminal
		}
		return nil
	})
	if err != nil && !errors.Is(err, errAudioTerminal) {
		return err
	}
	if !terminal || !received {
		return fmt.Errorf("transcription stream missing result or terminal")
	}
	return requestContext(local).Err()
}

func openAIAudioSpeechBody(name *string, text string, config *TTSConfig, stream bool) (map[string]any, string, string, error) {
	body, format, err := openAISpeechBody(name, text, config, stream)
	if err != nil {
		return nil, "", "", err
	}
	delete(body, "stream")
	if err := requireSpeechVoice(body); err != nil {
		return nil, "", "", err
	}
	mode := "audio"
	if value, ok := body["stream_format"]; ok {
		next, valid := value.(string)
		if !valid || next != "audio" && next != "sse" {
			return nil, "", "", fmt.Errorf("invalid speech stream_format")
		}
		mode = next
	}
	if !stream && mode == "sse" {
		return nil, "", "", fmt.Errorf("SSE speech requires streaming sender")
	}
	if stream {
		body["stream_format"] = mode
	}
	return body, format, mode, nil
}
func (o *OpenAIModel) AudioSpeech(name *string, text string, api *APIConfig, config *TTSConfig) (*TTSResponse, error) {
	body, format, _, err := openAIAudioSpeechBody(name, text, config, false)
	if err != nil {
		return nil, err
	}
	local, cancel := modelLongOpConfig(api)
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
	if err = requestContext(local).Err(); err != nil {
		return nil, err
	}
	return result, nil
}
func (o *OpenAIModel) AudioSpeechWithSender(name *string, text string, api *APIConfig, config *TTSConfig, send func(*TTSResponse) error) error {
	if send == nil {
		return fmt.Errorf("speech sender required")
	}
	body, format, mode, err := openAIAudioSpeechBody(name, text, config, true)
	if err != nil {
		return err
	}
	local, cancel := modelStreamConfig(api)
	defer cancel()
	accept := ""
	if mode == "sse" {
		accept = "text/event-stream"
	}
	resp, err := mediaJSONRequest(o.httpClient, o.BaseURL, o.URLSuffix.TTS, local, body, "", mediaHTTPOptions{accept: accept})
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if mode != "sse" && !strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream") {
		return streamSpeechBytes(resp, format, local, send)
	}
	total, terminal := 0, false
	err = scanMediaSSE(resp, func(_ string, raw []byte) error {
		if err := requestContext(local).Err(); err != nil {
			return err
		}
		var event struct {
			Type  string          `json:"type"`
			Audio *string         `json:"audio"`
			Error json.RawMessage `json:"error"`
		}
		if err := json.Unmarshal(raw, &event); err != nil {
			return fmt.Errorf("invalid speech SSE")
		}
		if len(event.Error) > 0 && string(event.Error) != "null" || event.Type == "error" {
			return fmt.Errorf("speech provider error")
		}
		switch event.Type {
		case "speech.audio.delta":
			if event.Audio == nil {
				return fmt.Errorf("missing speech SSE audio")
			}
			audio, err := DecodeMediaData(*event.Audio)
			if err != nil {
				return err
			}
			if total+len(audio) > MaxMediaBytes {
				return fmt.Errorf("speech stream exceeds limit")
			}
			if err := send(&TTSResponse{Audio: audio, Format: format}); err != nil {
				return err
			}
			total += len(audio)
		case "speech.audio.done":
			terminal = true
			return errAudioTerminal
		default:
			return fmt.Errorf("unknown speech SSE event")
		}
		return nil
	})
	if err != nil && !errors.Is(err, errAudioTerminal) {
		return err
	}
	if !terminal || total == 0 {
		return fmt.Errorf("speech stream missing audio or terminal")
	}
	return requestContext(local).Err()
}

var _ ASRDriver = (*OpenAIModel)(nil)
var _ ASRStreamDriver = (*OpenAIModel)(nil)
var _ TTSDriver = (*OpenAIModel)(nil)
var _ TTSStreamDriver = (*OpenAIModel)(nil)
