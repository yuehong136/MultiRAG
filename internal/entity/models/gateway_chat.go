package models

import "fmt"

// These gateways share transport and validation, while retaining their own catalogs.
func newGatewayChatDriver(urls map[string]string, suffix URLSuffix, provider string) *openAIChatDriver {
	d := newOpenAIChatDriver(urls, suffix, provider, gatewayChatBody)
	d.strictDiscovery, d.strictStream, d.terminalWithoutDelta = true, true, true
	d.rejectNonzeroCode = true
	if provider != "tokenpony" {
		d.reasoningAlias, d.reasoningFallback, d.rootFinishReason = true, true, true
	}
	return d
}

func gatewayChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	if stream && config != nil && config.Stream != nil && !*config.Stream {
		return nil, fmt.Errorf("stream must be true")
	}
	body, err := openAIChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	delete(body, "reasoning_effort")
	return body, nil
}
