// Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
// Licensed under the Apache License, Version 2.0.
package models

import (
	"encoding/json"
	"fmt"
	"strings"
)

var sparkModelVersions = map[string]string{
	"Spark-Max":       "generalv3.5",
	"Spark-Max-32K":   "max-32k",
	"Spark-Lite":      "lite",
	"Spark-Pro":       "generalv3",
	"Spark-Pro-128K":  "pro-128k",
	"Spark-4.0-Ultra": "4.0Ultra",
}

type XunFeiModel struct{ *openAIChatDriver }

func NewXunFeiModel(urls map[string]string, suffix URLSuffix) *XunFeiModel {
	d := newOpenAIChatDriver(urls, suffix, "xunfei", xunFeiChatBody)
	d.waitForStreamEOF = true
	d.rejectNonzeroCode = true
	return &XunFeiModel{d}
}

func xunFeiChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	if version, ok := sparkModelVersions[name]; ok {
		name = version
	}
	body, err := openAIChatBody(name, messages, config, stream)
	if err != nil {
		return nil, err
	}
	if config != nil && config.Thinking != nil {
		kind := "disabled"
		if *config.Thinking {
			kind = "enabled"
		}
		body["thinking"] = map[string]any{"type": kind}
	}
	return body, nil
}

func sparkAPIConfig(api *APIConfig) (*APIConfig, error) {
	if api == nil || api.APIKey == nil || strings.TrimSpace(*api.APIKey) == "" {
		return nil, fmt.Errorf("xunfei: API key is required")
	}
	key := strings.TrimSpace(*api.APIKey)
	if strings.HasPrefix(key, "{") {
		var bundle struct {
			Password *string `json:"spark_api_password"`
		}
		if err := json.Unmarshal([]byte(key), &bundle); err != nil || bundle.Password == nil || strings.TrimSpace(*bundle.Password) == "" {
			return nil, fmt.Errorf("xunfei: credential bundle requires spark_api_password")
		}
		key = strings.TrimSpace(*bundle.Password)
	}
	local := *api
	local.APIKey = &key
	return &local, nil
}

// Legacy Spark versions use v1; spark-x uses the catalog's v2 endpoint. Copy
// only the request driver so parallel calls never mutate instance settings.
func (x *XunFeiModel) driverForModel(name string) *openAIChatDriver {
	legacy := false
	for alias, version := range sparkModelVersions {
		if name == alias || name == version {
			legacy = true
			break
		}
	}
	if !legacy || strings.Trim(x.URLSuffix.Chat, "/") != "v2/chat/completions" {
		return x.openAIChatDriver
	}
	d := *x.openAIChatDriver
	model := *x.DummyModel
	model.URLSuffix.Chat = "v1/chat/completions"
	d.DummyModel = &model
	return &d
}

func (x *XunFeiModel) ChatWithMessages(name string, api *APIConfig, messages []Message, config *ChatConfig) (*ChatResponse, error) {
	local, err := sparkAPIConfig(api)
	if err != nil {
		return nil, err
	}
	return x.driverForModel(name).ChatWithMessages(name, local, messages, config)
}

func (x *XunFeiModel) ChatStreamlyWithMessages(name string, messages []Message, api *APIConfig, config *ChatConfig, send func(*string, *string) error) error {
	local, err := sparkAPIConfig(api)
	if err != nil {
		return err
	}
	return x.driverForModel(name).ChatStreamlyWithMessages(name, messages, local, config, send)
}

func (x *XunFeiModel) ChatStreamlyWithSender(name, message *string, api *APIConfig, config *ChatConfig, send func(*string, *string) error) error {
	if name == nil || message == nil {
		return fmt.Errorf("xunfei: model and message are required")
	}
	return x.ChatStreamlyWithMessages(*name, []Message{{Role: "user", Content: *message}}, api, config, send)
}

// Spark exposes no models endpoint in this catalog. This is local discovery,
// not credential verification; CheckConnection performs an authenticated call.
func (x *XunFeiModel) ListModels(api *APIConfig) ([]string, error) {
	if err := requestContext(api).Err(); err != nil {
		return nil, err
	}
	return []string{"spark-x"}, nil
}

func (x *XunFeiModel) CheckConnection(api *APIConfig) error {
	limit := 1
	_, err := x.ChatWithMessages("Spark-Lite", api, []Message{{Role: "user", Content: "Hi"}}, &ChatConfig{MaxTokens: &limit})
	return err
}

var _ ModelDriver = (*XunFeiModel)(nil)
