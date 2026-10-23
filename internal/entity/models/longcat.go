// Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
// Licensed under the Apache License, Version 2.0.
package models

import "fmt"

// LongCatModel implements chat and sender streaming without inventing a paid
// connectivity probe or a discovery endpoint absent from this provider batch.
type LongCatModel struct{ *openAIChatDriver }

func NewLongCatModel(urls map[string]string, suffix URLSuffix) *LongCatModel {
	d := newOpenAIChatDriver(urls, suffix, "longcat", longCatChatBody)
	d.waitForStreamEOF = true
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
func (l *LongCatModel) ListModels(api *APIConfig) ([]string, error) {
	return nil, fmt.Errorf("longcat: model discovery is unsupported")
}
func (l *LongCatModel) CheckConnection(api *APIConfig) error {
	return fmt.Errorf("longcat: connection probing is unsupported")
}

var _ ModelDriver = (*LongCatModel)(nil)
