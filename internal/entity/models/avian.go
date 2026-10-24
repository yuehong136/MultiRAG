package models

import (
	"fmt"
	"maps"
	"net/http"
	"strings"
	"time"
)

// AvianModel uses the hosted chat transport and Avian's bare discovery array.
type AvianModel struct{ *openAIChatDriver }

func NewAvianModel(urls map[string]string, suffix URLSuffix) *AvianModel {
	base := maps.Clone(urls)
	for region, endpoint := range base {
		base[region] = strings.TrimSuffix(strings.TrimRight(strings.TrimSpace(endpoint), "/"), "/v1")
	}
	d := newOpenAIChatDriver(base, suffix, "avian", hostedChatBody)
	d.reasoningAlias, d.strictStream, d.streamIdleTimeout = true, true, 60*time.Second
	d.rootFinishReason = true
	d.reasoningFallback = true
	d.terminalWithoutDelta = true
	d.httpClient.CheckRedirect = rejectModelRedirect
	return &AvianModel{d}
}

func (a *AvianModel) ListModels(api *APIConfig) ([]string, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := a.request(http.MethodGet, a.URLSuffix.Models, local, nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var entries *[]struct {
		ID string `json:"id"`
	}
	if err := decodeBoundedModelJSON(resp.Body, &entries); err != nil || entries == nil {
		return nil, fmt.Errorf("avian: invalid model list")
	}
	names, seen := make([]string, 0, len(*entries)), map[string]bool{}
	for _, item := range *entries {
		if item.ID == "" || strings.TrimSpace(item.ID) != item.ID || seen[item.ID] {
			return nil, fmt.Errorf("avian: invalid or duplicate model ID")
		}
		seen[item.ID] = true
		names = append(names, item.ID)
	}

	err = requestContext(local).Err()
	return names, err
}

func (a *AvianModel) CheckConnection(api *APIConfig) error {
	_, err := a.ListModels(api)
	return err
}

var _ ModelDriver = (*AvianModel)(nil)

// hostedChatBody keeps an explicit stream=false out of sender-based calls.
func hostedChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	if stream && config != nil && config.Stream != nil && !*config.Stream {
		return nil, fmt.Errorf("hosted chat: stream must be true")
	}
	return openAIChatBody(name, messages, config, stream)
}
