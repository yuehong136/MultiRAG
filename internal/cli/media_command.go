package cli

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"multirag/internal/entity/models"
	"os"
	"path/filepath"
	"strings"
)

func readMediaFile(path string) ([]byte, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("read media: %w", err)
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, models.MaxMediaBytes+1))
	if err != nil {
		return nil, err
	}
	if err := models.ValidateMediaBytes(data); err != nil {
		return nil, err
	}
	return data, nil
}
func (c *MultiRAGClient) MediaCommand(cmd *Command, kind string) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}
	if c.HTTPClient.APIKey == "" && c.HTTPClient.LoginToken == "" {
		return nil, fmt.Errorf("please login first")
	}
	name, _ := cmd.Params["composite_model_name"].(string)
	if name == "" && c.CurrentModel != nil {
		name = c.CurrentModel.Model + "@" + c.CurrentModel.Instance + "@" + c.CurrentModel.Provider
	}
	parts := strings.Split(name, "@")
	if len(parts) != 3 {
		return nil, fmt.Errorf("model must be model@instance@provider")
	}
	for _, p := range parts {
		if strings.TrimSpace(p) == "" {
			return nil, fmt.Errorf("empty model identifier")
		}
	}
	payload := map[string]any{"model_name": parts[0], "instance_name": parts[1], "provider_name": parts[2]}
	path := "/audio/transcriptions"
	switch kind {
	case "tts":
		path = "/audio/speech"
		text, _ := cmd.Params["text"].(string)
		if err := models.ValidateSpeechText(text); err != nil {
			return nil, err
		}
		payload["text"] = text
	case "asr", "ocr":
		field := "audio_file"
		if kind == "ocr" {
			field = "file"
			path = "/file/ocr"
		}
		filename, ok := cmd.Params[field].(string)
		if !ok || filename == "" {
			return nil, fmt.Errorf("media file required")
		}
		data, err := readMediaFile(filename)
		if err != nil {
			return nil, err
		}
		if kind == "asr" {
			payload["file"] = data
			format := strings.ToLower(strings.TrimPrefix(filepath.Ext(filename), "."))
			if format != "wav" && format != "mp3" {
				return nil, fmt.Errorf("audio file must be wav or mp3")
			}
			payload["format"] = format
		} else {
			payload["content"] = data
		}
	default:
		return nil, fmt.Errorf("unknown media operation")
	}
	stream, _ := cmd.Params["stream"].(bool)
	if stream {
		if kind == "ocr" {
			return nil, fmt.Errorf("OCR streaming is unsupported")
		}
		payload["stream"] = true
		reader, err := c.HTTPClient.RequestStream("POST", path, "api", nil, payload)
		if err != nil {
			return nil, err
		}
		defer reader.Close()
		return readMediaStream(reader, kind)
	}
	response, err := c.HTTPClient.RequestBounded("POST", path, "api", nil, payload, int64(base64.StdEncoding.EncodedLen(models.MaxMediaBytes)+64*1024))
	if err != nil {
		return nil, err
	}
	if response.StatusCode != 200 {
		return nil, fmt.Errorf("media inference: HTTP %d", response.StatusCode)
	}
	var envelope struct {
		Code    *int            `json:"code"`
		Data    json.RawMessage `json:"data"`
		Message string          `json:"message"`
	}
	if err := json.Unmarshal(response.Body, &envelope); err != nil {
		return nil, fmt.Errorf("invalid media response")
	}
	if envelope.Code == nil || *envelope.Code != 0 {
		return nil, fmt.Errorf("media inference failed: %s", envelope.Message)
	}
	row, err := mediaResult(kind, envelope.Data)
	if err != nil {
		return nil, err
	}
	return &CommonDataResponse{Code: 0, Data: row}, nil
}
func mediaResult(kind string, raw []byte) (map[string]any, error) {
	if kind == "tts" {
		var encoded struct {
			Audio  string `json:"audio"`
			Format string `json:"format"`
		}
		if err := json.Unmarshal(raw, &encoded); err != nil {
			return nil, fmt.Errorf("invalid speech response")
		}
		audio, err := models.DecodeMediaData(encoded.Audio)
		if err != nil {
			return nil, err
		}
		r := models.TTSResponse{Audio: audio, Format: encoded.Format}
		if err := models.ValidateTTSResponse(&r); err != nil {
			return nil, err
		}
		return map[string]any{"audio": base64.StdEncoding.EncodeToString(r.Audio), "format": r.Format}, nil
	}
	var r models.ASRResponse
	if err := json.Unmarshal(raw, &r); err != nil {
		return nil, fmt.Errorf("invalid text response")
	}
	if err := models.ValidateASRResponse(&r); err != nil {
		return nil, err
	}
	return map[string]any{"text": *r.Text}, nil
}
func readMediaStream(reader io.Reader, kind string) (ResponseIf, error) {
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 64*1024), base64.StdEncoding.EncodedLen(models.MaxMediaBytes)+64*1024)
	event := ""
	var data strings.Builder
	done := false
	messages := 0
	var text strings.Builder
	var audio []byte
	format := ""
	dispatch := func() error {
		if data.Len() == 0 {
			event = ""
			return nil
		}
		raw := data.String()
		defer func() { data.Reset(); event = "" }()
		if done {
			return fmt.Errorf("media event after completion")
		}
		if event == "error" {
			var e struct {
				Code    int
				Message string
			}
			if err := json.Unmarshal([]byte(raw), &e); err != nil || e.Code == 0 {
				return fmt.Errorf("invalid media error event")
			}
			return fmt.Errorf("media inference failed (%d): %s", e.Code, e.Message)
		}
		if event == "done" {
			if raw != "[DONE]" {
				return fmt.Errorf("invalid media completion")
			}
			done = true
			return nil
		}
		if event != "message" {
			return fmt.Errorf("unexpected media event")
		}
		row, err := mediaResult(kind, []byte(raw))
		if err != nil {
			return err
		}
		messages++
		if kind == "tts" {
			part, err := base64.StdEncoding.Strict().DecodeString(row["audio"].(string))
			if err != nil {
				return err
			}
			next := row["format"].(string)
			if format != "" && format != next {
				return fmt.Errorf("speech format changed")
			}
			format = next
			if len(audio)+len(part) > models.MaxMediaBytes {
				return fmt.Errorf("speech stream too large")
			}
			audio = append(audio, part...)
		} else {
			next := row["text"].(string)
			if text.Len()+len(next) > models.MaxMediaBytes {
				return fmt.Errorf("text stream too large")
			}
			text.WriteString(next)
		}
		return nil
	}
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			if err := dispatch(); err != nil {
				return nil, err
			}
			continue
		}
		if strings.HasPrefix(line, ":") {
			continue
		}
		if strings.HasPrefix(line, "event:") {
			event = strings.TrimSpace(strings.TrimPrefix(line, "event:"))
		}
		if strings.HasPrefix(line, "data:") {
			part := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
			if data.Len()+len(part)+1 > base64.StdEncoding.EncodedLen(models.MaxMediaBytes)+64*1024 {
				return nil, fmt.Errorf("media stream frame exceeds limit")
			}
			if data.Len() > 0 {
				data.WriteByte('\n')
			}
			data.WriteString(part)
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	if err := dispatch(); err != nil {
		return nil, err
	}
	if !done {
		return nil, fmt.Errorf("media stream ended before completion: %w", io.ErrUnexpectedEOF)
	}
	row := map[string]any{"text": text.String()}
	if kind == "tts" {
		if len(audio) == 0 {
			return nil, fmt.Errorf("empty speech stream")
		}
		row = map[string]any{"audio": base64.StdEncoding.EncodeToString(audio), "format": format}
	} else if messages == 0 {
		return nil, fmt.Errorf("empty transcription stream")
	}
	return &CommonDataResponse{Code: 0, Data: row}, nil
}
