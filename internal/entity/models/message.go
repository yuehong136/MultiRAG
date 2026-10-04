package models

import (
	"encoding/base64"
	"fmt"
	"net/url"
	"strings"
)

// ContentParts validates and normalizes the two supported content-array representations.
// Unknown part types fail explicitly instead of silently losing user input.
func ContentParts(content any) ([]map[string]any, error) {
	var parts []map[string]any
	switch value := content.(type) {
	case []map[string]any:
		parts = value
	case []any:
		for _, item := range value {
			part, ok := item.(map[string]any)
			if !ok {
				return nil, fmt.Errorf("content parts must be objects")
			}
			parts = append(parts, part)
		}
	default:
		return nil, fmt.Errorf("content must be a string or an array of parts")
	}
	if len(parts) == 0 {
		return nil, fmt.Errorf("content parts cannot be empty")
	}
	for _, part := range parts {
		switch part["type"] {
		case "text":
			text, ok := part["text"].(string)
			if !ok || text == "" {
				return nil, fmt.Errorf("text part requires nonempty text")
			}
		case "image_url":
			img, ok := part["image_url"].(map[string]any)
			if !ok {
				return nil, fmt.Errorf("image_url must be an object")
			}
			raw, ok := img["url"].(string)
			if !ok {
				return nil, fmt.Errorf("image_url requires a URL")
			}
			if strings.HasPrefix(raw, "data:") {
				if _, _, err := imageData(raw); err != nil {
					return nil, err
				}
			} else {
				u, err := url.Parse(raw)
				if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" || u.User != nil {
					return nil, fmt.Errorf("image URL must use HTTP(S) or a base64 image data URL")
				}
			}
			if detail, ok := img["detail"]; ok && detail != "auto" && detail != "low" && detail != "high" {
				return nil, fmt.Errorf("invalid image detail")
			}
		default:
			return nil, fmt.Errorf("unsupported content part type")
		}
	}
	return parts, nil
}

func imageData(raw string) (string, []byte, error) {
	header, encoded, ok := strings.Cut(raw, ",")
	if !ok || !strings.HasPrefix(header, "data:image/") || !strings.HasSuffix(header, ";base64") {
		return "", nil, fmt.Errorf("invalid image data URL")
	}
	mime := strings.TrimSuffix(strings.TrimPrefix(header, "data:"), ";base64")
	data, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil || len(data) == 0 {
		return "", nil, fmt.Errorf("invalid base64 image data")
	}
	return mime, data, nil
}

// ValidateMessages is shared by HTTP, bound models, and direct provider callers.
func ValidateMessages(messages []Message) error {
	if len(messages) == 0 {
		return fmt.Errorf("messages cannot be empty")
	}
	for i, message := range messages {
		switch message.Role {
		case "system", "user", "assistant", "model":
		default:
			return fmt.Errorf("message %d has unsupported role", i)
		}
		if message.ReasoningContent != nil && message.Role != "assistant" && message.Role != "model" {
			return fmt.Errorf("reasoning_content requires an assistant role")
		}
		if text, ok := message.Content.(string); ok {
			if strings.TrimSpace(text) == "" {
				return fmt.Errorf("message %d has empty content", i)
			}
		} else if _, err := ContentParts(message.Content); err != nil {
			return fmt.Errorf("message %d: %w", i, err)
		}
	}
	return nil
}

// ValidateTextMessages defines the current streaming boundary for every provider.
func ValidateTextMessages(messages []Message) error {
	if err := ValidateMessages(messages); err != nil {
		return err
	}
	for _, message := range messages {
		if _, ok := message.Content.(string); !ok {
			return fmt.Errorf("streaming with multimodal content is not supported")
		}
	}
	return nil
}

// ValidateChatResponse protects all consumers from nil provider responses.
func ValidateChatResponse(response *ChatResponse) error {
	if response == nil || response.Answer == nil || *response.Answer == "" {
		return fmt.Errorf("model returned no text answer")
	}
	return nil
}
