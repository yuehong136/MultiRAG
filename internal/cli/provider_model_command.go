package cli

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
)

func (c *MultiRAGClient) AddCustomModel(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}
	if c.HTTPClient.APIKey == "" && c.HTTPClient.LoginToken == "" {
		return nil, fmt.Errorf("please login first")
	}
	payload := make(map[string]interface{})
	for _, field := range []string{"provider_name", "instance_name", "model_name"} {
		value, ok := cmd.Params[field].(string)
		if !ok || value == "" {
			return nil, fmt.Errorf("%s is required", field)
		}
		payload[field] = value
	}
	if types, ok := cmd.Params["model_types"].([]string); ok {
		if len(types) == 0 {
			return nil, fmt.Errorf("model_types must not be empty")
		}
		payload["model_types"] = types
		payload["model_type"] = types[0]
	} else if typ, ok := cmd.Params["model_type"].(string); ok && typ != "" {
		payload["model_type"] = typ
	} else {
		return nil, fmt.Errorf("model capabilities required")
	}
	tokens, ok := cmd.Params["max_tokens"].(int)
	if !ok || tokens <= 0 {
		return nil, fmt.Errorf("positive max_tokens required")
	}
	payload["max_tokens"] = tokens
	if value, ok := cmd.Params["support_think"].(bool); ok {
		payload["thinking"] = value
	}
	path := fmt.Sprintf("/providers/%s/instances/%s/models", url.PathEscape(payload["provider_name"].(string)), url.PathEscape(payload["instance_name"].(string)))
	resp, err := c.HTTPClient.Request("POST", path, true, "web", nil, payload)
	if err != nil {
		return nil, err
	}
	return modelMutationResponse(resp.StatusCode, resp.Body, "add custom model")
}

func (c *MultiRAGClient) DropInstanceModel(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}
	if c.HTTPClient.APIKey == "" && c.HTTPClient.LoginToken == "" {
		return nil, fmt.Errorf("please login first")
	}
	values := make(map[string]string)
	for _, field := range []string{"provider_name", "instance_name", "model_name"} {
		value, ok := cmd.Params[field].(string)
		if !ok || strings.TrimSpace(value) == "" {
			return nil, fmt.Errorf("%s is required", field)
		}
		values[field] = value
	}
	path := fmt.Sprintf("/providers/%s/instances/%s/models", url.PathEscape(values["provider_name"]), url.PathEscape(values["instance_name"]))
	resp, err := c.HTTPClient.Request("DELETE", path, true, "web", nil, map[string]interface{}{"models": []string{values["model_name"]}})
	if err != nil {
		return nil, err
	}
	return modelMutationResponse(resp.StatusCode, resp.Body, "drop instance model")
}

func modelMutationResponse(status int, body []byte, operation string) (*SimpleResponse, error) {
	if status != http.StatusOK {
		return nil, fmt.Errorf("%s: HTTP %d", operation, status)
	}
	var result struct {
		Code    *int   `json:"code"`
		Message string `json:"message"`
	}
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("%s: invalid response", operation)
	}
	if result.Code == nil {
		return nil, fmt.Errorf("%s: missing result code", operation)
	}
	if *result.Code != 0 {
		return nil, fmt.Errorf("%s failed (code %d)", operation, *result.Code)
	}
	return &SimpleResponse{Code: *result.Code, Message: result.Message}, nil
}
