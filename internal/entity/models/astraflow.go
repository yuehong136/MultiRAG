package models

import "time"

// AstraflowModel implements the frozen hosted chat and wrapped discovery contract.
type AstraflowModel struct{ *openAIChatDriver }

func NewAstraflowModel(urls map[string]string, suffix URLSuffix) *AstraflowModel {
	d := newOpenAIChatDriver(urls, suffix, "astraflow", hostedChatBody)
	d.reasoningAlias, d.strictDiscovery, d.strictStream = true, true, true
	d.streamIdleTimeout = 60 * time.Second
	d.reasoningFallback = true
	d.terminalWithoutDelta = true
	d.httpClient.CheckRedirect = rejectModelRedirect
	return &AstraflowModel{d}
}

var _ ModelDriver = (*AstraflowModel)(nil)
