package models

import (
	"fmt"
	"maps"
	"strings"
	"time"
)

// XiaomiModel implements MiMo chat using the shared bounded, cancellable transport.
type XiaomiModel struct{ *openAIChatDriver }

func NewXiaomiModel(urls map[string]string, suffix URLSuffix) *XiaomiModel {
	base := maps.Clone(urls)
	suffix.Chat = strings.Trim(strings.TrimSpace(suffix.Chat), "/")
	if suffix.Chat == "" {
		suffix.Chat = "v1/chat/completions"
	}
	for region, endpoint := range base {
		endpoint = strings.TrimRight(strings.TrimSpace(endpoint), "/")
		if strings.HasPrefix(suffix.Chat, "v1/") {
			endpoint = strings.TrimSuffix(endpoint, "/v1")
		}
		base[region] = endpoint
	}
	if base["default"] == "" && base[""] != "" {
		base["default"] = base[""]
	}
	driver := newOpenAIChatDriver(base, suffix, "xiaomi", xiaomiChatBody)
	driver.authHeader, driver.accept = "api-key", "application/json"
	driver.strictStream, driver.terminalWithoutDelta, driver.sseEvents = true, true, true
	driver.streamIdleTimeout = 60 * time.Second
	driver.httpClient.CheckRedirect = rejectModelRedirect
	return &XiaomiModel{driver}
}

func (m *XiaomiModel) NewInstance(urls map[string]string) ModelDriver {
	return NewXiaomiModel(urls, m.URLSuffix)
}

func xiaomiChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	body, err := validatedHostedChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	if value, exists := body["max_tokens"]; exists {
		body["max_completion_tokens"] = value
		delete(body, "max_tokens")
	}
	if config != nil && config.Thinking != nil {
		thinking := "disabled"
		if *config.Thinking {
			thinking = "enabled"
		}
		body["thinking"] = map[string]any{"type": thinking}
	}
	return body, nil
}

// This catalog has no model discovery endpoint. A successful chat does not
// establish support for discovery, tools, audio or other model capabilities.
func (m *XiaomiModel) ListModels(api *APIConfig) ([]string, error) {
	return nil, fmt.Errorf("%w: xiaomi model discovery", ErrUnsupportedCapability)
}

func (m *XiaomiModel) CheckConnection(api *APIConfig) error {
	return fmt.Errorf("%w: xiaomi connection check", ErrUnsupportedCapability)
}

var _ ModelDriver = (*XiaomiModel)(nil)
