package models

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws/protocol/eventstream"
)

func (b *BedrockModel) ChatStreamlyWithSender(name, message *string, api *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if name == nil || message == nil {
		return fmt.Errorf("bedrock: model and message are required")
	}
	return b.ChatStreamlyWithMessages(*name, []Message{{Role: "user", Content: *message}}, api, config, sender)
}
func (b *BedrockModel) ChatStreamlyWithMessages(name string, messages []Message, api *APIConfig, config *ChatConfig, sender func(*string, *string) error) (streamErr error) {
	if sender == nil {
		return fmt.Errorf("bedrock: sender is required")
	}
	if config != nil && config.Stream != nil && !*config.Stream {
		return fmt.Errorf("bedrock: stream must be true")
	}
	body, err := bedrockChatBody(name, messages, config)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithCancelCause(requestContext(api))
	defer cancel(nil)
	local := APIConfig{Context: ctx}
	if api != nil {
		local = *api
		local.Context = ctx
	}
	timeout := b.streamIdleTimeout
	if timeout <= 0 {
		timeout = 60 * time.Second
	}
	idleErr := fmt.Errorf("bedrock: stream idle timeout: %w", context.DeadlineExceeded)
	timer := time.AfterFunc(timeout, func() { cancel(idleErr) })
	defer func() {
		timer.Stop()
		if context.Cause(ctx) == idleErr {
			streamErr = idleErr
		}
	}()
	operation := bedrockSuffix(b.URLSuffix.AsyncChat, bedrockSuffix(b.URLSuffix.Chat, "converse")+"-stream")
	resp, err := b.request(&local, name, operation, body, true, false)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	typ, _, err := mime.ParseMediaType(resp.Header.Get("Content-Type"))
	if err != nil || typ != "application/vnd.amazon.eventstream" {
		return fmt.Errorf("bedrock: invalid stream content type")
	}
	reader := &activeModelStream{ReadCloser: resp.Body, active: func() { timer.Reset(timeout) }}
	if err := decodeBedrockEventStream(ctx, reader, sender); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	done := "[DONE]"
	return sender(&done, nil)
}

func bedrockEventHeaders(headers eventstream.Headers) (string, string, error) {
	values := map[string]string{}
	for _, header := range headers {
		if header.Name != ":event-type" && header.Name != ":message-type" {
			continue
		}
		value, ok := header.Value.Get().(string)
		if !ok || value == "" || values[header.Name] != "" {
			return "", "", fmt.Errorf("bedrock: invalid event header")
		}
		values[header.Name] = value
	}
	if values[":message-type"] == "" {
		return "", "", fmt.Errorf("bedrock: missing message type")
	}
	return values[":event-type"], values[":message-type"], nil
}

func decodeBedrockEventStream(ctx context.Context, reader io.Reader, sender func(*string, *string) error) error {
	decoder := eventstream.NewDecoder()
	started, terminal, answered := false, false, false
	blocks, closed := map[int]bool{}, map[int]bool{}
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		message, err := decoder.Decode(reader, nil)
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			if err == io.EOF && terminal {
				return nil
			}
			return fmt.Errorf("bedrock: truncated or invalid event stream: %w", err)
		}
		event, kind, err := bedrockEventHeaders(message.Headers)
		if err != nil {
			return err
		}
		if kind == "exception" || kind == "error" || strings.HasSuffix(event, "Exception") {
			return fmt.Errorf("bedrock: provider stream exception")
		}
		if kind != "event" || event == "" {
			return fmt.Errorf("bedrock: invalid event type")
		}
		var payload struct {
			Role  string `json:"role"`
			Index *int   `json:"contentBlockIndex"`
			Delta *struct {
				Text      *string         `json:"text"`
				Reasoning json.RawMessage `json:"reasoningContent"`
				Tool      json.RawMessage `json:"toolUse"`
			} `json:"delta"`
			Start      json.RawMessage `json:"start"`
			StopReason string          `json:"stopReason"`
			Error      json.RawMessage `json:"error"`
		}
		if err := json.Unmarshal(message.Payload, &payload); err != nil {
			return fmt.Errorf("bedrock: malformed event JSON")
		}
		if len(payload.Error) > 0 && string(payload.Error) != "null" {
			return fmt.Errorf("bedrock: provider stream error")
		}
		if terminal && event != "metadata" {
			return fmt.Errorf("bedrock: event after messageStop")
		}
		switch event {
		case "messageStart":
			if started || payload.Role != "assistant" {
				return fmt.Errorf("bedrock: invalid messageStart")
			}
			started = true
		case "contentBlockStart":
			// Converse emits starts for unsupported tool blocks. Never silently discard them.
			return fmt.Errorf("bedrock: contentBlockStart modality is unsupported")
		case "contentBlockDelta":
			if !started || payload.Index == nil || *payload.Index < 0 || closed[*payload.Index] || payload.Delta == nil || payload.Delta.Text == nil || len(payload.Delta.Tool) > 0 || len(payload.Delta.Reasoning) > 0 {
				return fmt.Errorf("bedrock: invalid or unsupported content delta")
			}
			blocks[*payload.Index] = true
			text := *payload.Delta.Text
			if err := ctx.Err(); err != nil {
				return err
			}
			if text != "" {
				if err := sender(&text, nil); err != nil {
					return err
				}
				answered = answered || strings.TrimSpace(text) != ""
			}
		case "contentBlockStop":
			if payload.Index == nil || !blocks[*payload.Index] || closed[*payload.Index] {
				return fmt.Errorf("bedrock: invalid contentBlockStop")
			}
			closed[*payload.Index] = true
		case "messageStop":
			if !started || !answered {
				return fmt.Errorf("bedrock: stream returned no text answer")
			}
			if err := validBedrockStop(payload.StopReason); err != nil {
				return err
			}
			for index := range blocks {
				if !closed[index] {
					return fmt.Errorf("bedrock: unclosed content block")
				}
			}
			terminal = true
		case "metadata":
			if !terminal {
				return fmt.Errorf("bedrock: metadata before terminal")
			}
		default:
			return fmt.Errorf("bedrock: unknown stream event")
		}
	}
}
