package cli

import (
	"encoding/json"
	"fmt"
	"net/url"
)

func (c *MultiRAGClient) AddCustomModel(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}
	if c.HTTPClient.APIKey == "" && c.HTTPClient.LoginToken == "" {
		return nil, fmt.Errorf("please login first")
	}
	payload := make(map[string]interface{})
	for _, field := range []string{"provider_name", "instance_name", "model_name", "model_type"} {
		value, ok := cmd.Params[field].(string)
		if !ok || value == "" {
			return nil, fmt.Errorf("%s is required", field)
		}
		payload[field] = value
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
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("add custom model: HTTP %d", resp.StatusCode)
	}
	var result SimpleResponse
	if err := json.Unmarshal(resp.Body, &result); err != nil {
		return nil, err
	}
	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}
	return &result, nil
}
