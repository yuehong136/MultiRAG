package models

import (
	"context"
	"fmt"
	"slices"
	"time"

	"github.com/cloudwego/eino/components/model"
	"github.com/cloudwego/eino/schema"
)

// EinoChatModel binds Eino to an authorized model; each call copies request state.
// Tools are explicitly unavailable until the provider wire layer supports them.
type EinoChatModel struct {
	inner  *ChatModel
	config ChatConfig
}

var _ model.ToolCallingChatModel = (*EinoChatModel)(nil)

func NewEinoChatModel(cm *ChatModel, config *ChatConfig) *EinoChatModel {
	result := &EinoChatModel{inner: cm}
	if cm != nil {
		result.config = cm.ModelConfig
	}
	if config != nil {
		result.config = *config
	}
	return result
}
func (m *EinoChatModel) WithTools(tools []*schema.ToolInfo) (model.ToolCallingChatModel, error) {
	if m == nil {
		return nil, fmt.Errorf("nil chat model")
	}
	if len(tools) > 0 {
		return nil, fmt.Errorf("%w: tool calling", ErrUnsupportedCapability)
	}
	cp := *m
	return &cp, nil
}
func (m *EinoChatModel) request(ctx context.Context, msgs []*schema.Message, opts ...model.Option) (*ChatModel, []Message, error) {
	if ctx == nil || m == nil || m.inner == nil || m.inner.ModelDriver == nil || m.inner.ModelName == nil || m.inner.APIConfig == nil {
		return nil, nil, fmt.Errorf("authorized chat model and context required")
	}
	if err := ctx.Err(); err != nil {
		return nil, nil, err
	}
	options := model.GetCommonOptions(nil, opts...)
	if len(options.Tools) > 0 || len(options.DeferredTools) > 0 || options.ToolSearchTool != nil || options.ToolChoice != nil || options.AgenticToolChoice != nil || len(options.AllowedToolNames) > 0 {
		return nil, nil, fmt.Errorf("%w: tool calling", ErrUnsupportedCapability)
	}
	if options.Model != nil && *options.Model != *m.inner.ModelName {
		return nil, nil, fmt.Errorf("model override would bypass authorized binding")
	}
	cm := *m.inner
	api := *cm.APIConfig
	api.Context = ctx
	cm.APIConfig = &api
	cm.ModelConfig = m.config
	if options.Temperature != nil {
		v := float64(*options.Temperature)
		cm.ModelConfig.Temperature = &v
	}
	if options.TopP != nil {
		v := float64(*options.TopP)
		cm.ModelConfig.TopP = &v
	}
	if options.MaxTokens != nil {
		v := *options.MaxTokens
		cm.ModelConfig.MaxTokens = &v
	}
	if options.Stop != nil {
		v := slices.Clone(options.Stop)
		cm.ModelConfig.Stop = &v
	}
	messages := make([]Message, 0, len(msgs))
	for _, msg := range msgs {
		if msg == nil {
			return nil, nil, fmt.Errorf("nil chat message")
		}
		if len(msg.ToolCalls) > 0 || msg.ToolCallID != "" || len(msg.MultiContent) > 0 || len(msg.UserInputMultiContent) > 0 || len(msg.AssistantGenMultiContent) > 0 {
			return nil, nil, fmt.Errorf("%w: non-text Eino message", ErrUnsupportedCapability)
		}
		switch msg.Role {
		case schema.System, schema.User, schema.Assistant:
		default:
			return nil, nil, fmt.Errorf("%w: chat role %s", ErrUnsupportedCapability, msg.Role)
		}
		entry := Message{Role: string(msg.Role), Content: msg.Content}
		if msg.ReasoningContent != "" {
			reasoning := msg.ReasoningContent
			entry.ReasoningContent = &reasoning
		}
		messages = append(messages, entry)
	}
	if err := ValidateMessages(messages); err != nil {
		return nil, nil, err
	}
	return &cm, messages, nil
}
func (m *EinoChatModel) Generate(ctx context.Context, msgs []*schema.Message, opts ...model.Option) (*schema.Message, error) {
	cm, messages, err := m.request(ctx, msgs, opts...)
	if err != nil {
		return nil, err
	}
	response, err := cm.ChatWithMessages(messages, nil)
	if err != nil {
		return nil, err
	}
	out := &schema.Message{Role: schema.Assistant, Content: *response.Answer}
	if response.ReasoningContent != nil {
		out.ReasoningContent = *response.ReasoningContent
	}
	return out, nil
}
func (m *EinoChatModel) Stream(ctx context.Context, msgs []*schema.Message, opts ...model.Option) (*schema.StreamReader[*schema.Message], error) {
	if ctx == nil {
		return nil, fmt.Errorf("request context required")
	}
	callCtx, cancel := context.WithCancel(ctx)
	cm, messages, err := m.request(callCtx, msgs, opts...)
	if err != nil {
		cancel()
		return nil, err
	}
	type event struct {
		message *schema.Message
		err     error
	}
	events := make(chan event)
	sr, sw := schema.Pipe[*schema.Message](0)
	// Heartbeats stay inside the converting reader. A closed reader unblocks Send,
	// cancels a provider stalled before its next delta, and releases the pump.
	go func() {
		defer sw.Close()
		defer cancel()
		ticker := time.NewTicker(100 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case e, ok := <-events:
				if !ok {
					return
				}
				if sw.Send(e.message, e.err) {
					return
				}
			case <-ticker.C:
				if sw.Send(nil, nil) {
					return
				}
			case <-callCtx.Done():
				sw.Send(nil, callCtx.Err())
				return
			}
		}
	}()
	go func() {
		defer close(events)
		err := cm.ChatStreamlyWithSender(messages, nil, func(content, reasoning *string) error {
			if content != nil && *content == "[DONE]" {
				return nil
			}
			message := &schema.Message{Role: schema.Assistant}
			if content != nil {
				message.Content = *content
			}
			if reasoning != nil {
				message.ReasoningContent = *reasoning
			}
			select {
			case events <- event{message: message}:
				return nil
			case <-callCtx.Done():
				return callCtx.Err()
			}
		})
		if err != nil {
			select {
			case events <- event{err: err}:
			case <-callCtx.Done():
			}
		}
	}()
	return schema.StreamReaderWithConvert(sr, func(message *schema.Message) (*schema.Message, error) {
		if message == nil {
			return nil, schema.ErrNoValue
		}
		return message, nil
	}), nil
}
