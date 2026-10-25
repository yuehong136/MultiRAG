// Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
// Licensed under the Apache License, Version 2.0.
package models

import "strings"

// LongCatModel implements chat, sender streaming and authenticated model discovery.
type LongCatModel struct{ *openAIChatDriver }

func NewLongCatModel(urls map[string]string, suffix URLSuffix) *LongCatModel {
	suffix.Models = strings.Trim(strings.TrimSpace(suffix.Models), "/")
	d := newOpenAIChatDriver(urls, suffix, "longcat", longCatChatBody)
	d.waitForStreamEOF = true
	d.strictDiscovery = true
	d.httpClient.CheckRedirect = rejectModelRedirect
	return &LongCatModel{d}
}
func longCatChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]interface{}, error) {
	body, err := openAIChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	delete(body, "stop")
	return body, nil
}

var _ ModelDriver = (*LongCatModel)(nil)
