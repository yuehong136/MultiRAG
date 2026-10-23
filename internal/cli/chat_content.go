package cli

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"multirag/internal/entity/models"
	"net/http"
	"os"
	"path/filepath"
	"strings"
)

type chatInput struct{ Kind, Value string }

func chatContent(message string, raw any) (any, error) {
	var content any = message
	if json.Valid([]byte(message)) && strings.HasPrefix(strings.TrimSpace(message), "[") {
		var parts []any
		if err := json.Unmarshal([]byte(message), &parts); err != nil {
			return nil, err
		}
		content = parts
	}
	if raw == nil {
		return content, nil
	}
	inputs, ok := raw.([]chatInput)
	if !ok {
		return nil, fmt.Errorf("invalid chat inputs")
	}
	var parts []map[string]any
	if message != "" {
		if text, ok := content.(string); ok {
			parts = append(parts, map[string]any{"type": "text", "text": text})
		} else {
			parsed, err := models.ContentParts(content)
			if err != nil {
				return nil, err
			}
			parts = append(parts, parsed...)
		}
	}
	audioCount := 0
	for _, input := range inputs {
		value := input.Value
		if input.Kind == "input_audio" {
			audioCount++
			if audioCount > 1 {
				return nil, fmt.Errorf("only one audio file is supported")
			}
			format := strings.ToLower(strings.TrimPrefix(filepath.Ext(value), "."))
			if format != "wav" && format != "mp3" {
				return nil, fmt.Errorf("audio file must be wav or mp3")
			}
			data, err := readMediaFile(value)
			if err != nil {
				return nil, err
			}
			parts = append(parts, map[string]any{"type": "input_audio", "input_audio": map[string]any{"data": base64.StdEncoding.EncodeToString(data), "format": format}})
			continue
		}
		if input.Kind == "text" {
			parts = append(parts, map[string]any{"type": "text", "text": value})
			continue
		}
		if input.Kind == "image_url" && !strings.Contains(value, "://") && !strings.HasPrefix(value, "data:") {
			file, err := os.Open(value)
			if err != nil {
				return nil, fmt.Errorf("read image: %w", err)
			}
			data, err := io.ReadAll(io.LimitReader(file, 20*1024*1024+1))
			file.Close()
			if err != nil {
				return nil, err
			}
			if len(data) > 20*1024*1024 {
				return nil, fmt.Errorf("image exceeds 20 MiB")
			}
			mime := http.DetectContentType(data)
			if !strings.HasPrefix(mime, "image/") {
				return nil, fmt.Errorf("local file is not a supported image")
			}
			value = "data:" + mime + ";base64," + base64.StdEncoding.EncodeToString(data)
		}
		parts = append(parts, map[string]any{"type": input.Kind, input.Kind: map[string]any{"url": value}})
	}
	if _, err := models.ContentParts(parts); err != nil {
		return nil, err
	}
	return parts, nil
}
