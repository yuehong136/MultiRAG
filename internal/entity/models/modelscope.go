package models

import (
	"strings"
	"time"
)

// ModelScopeModel uses a tenant-supplied endpoint with optional deployment auth.
type ModelScopeModel struct{ *openAIChatDriver }

func NewModelScopeModel(urls map[string]string, suffix URLSuffix) *ModelScopeModel {
	normalized := make(map[string]string, len(urls))
	for region, base := range urls {
		normalized[region] = strings.TrimSuffix(strings.TrimRight(strings.TrimSpace(base), "/"), "/v1")
	}
	d := newGatewayChatDriver(normalized, suffix, "modelscope")
	d.buildBody = validatedHostedChatBody
	d.optionalAPIKey = true
	d.thinkingAlias = true
	d.sseEvents = true
	d.streamIdleTimeout = 60 * time.Second
	d.httpClient.CheckRedirect = rejectModelRedirect
	return &ModelScopeModel{d}
}
func (m *ModelScopeModel) NewInstance(urls map[string]string) ModelDriver {
	return NewModelScopeModel(urls, m.URLSuffix)
}

var _ ModelDriver = (*ModelScopeModel)(nil)
