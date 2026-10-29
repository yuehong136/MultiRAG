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
	for _, field := range []string{"provider_name", "instance_name"} {
		value, ok := cmd.Params[field].(string)
		if !ok || strings.TrimSpace(value) == "" {
			return nil, fmt.Errorf("%s is required", field)
		}
		payload[field] = value
	}
	models, ok := cmd.Params["models"].([]customModelDeclaration)
	if !ok || len(models) == 0 || len(models) > 100 {
		return nil, fmt.Errorf("between 1 and 100 models required")
	}
	seen := make(map[string]bool, len(models))
	for _, model := range models {
		if strings.TrimSpace(model.Name) == "" || seen[model.Name] || model.MaxTokens < 0 || len(model.Types) == 0 {
			return nil, fmt.Errorf("invalid or duplicate model declaration")
		}
		seen[model.Name] = true
		capabilities := make(map[string]bool, len(model.Types))
		for _, capability := range model.Types {
			switch capability {
			case "chat", "image2text", "embedding", "rerank", "speech2text", "tts", "ocr", "doc_parse":
			default:
				return nil, fmt.Errorf("unsupported model capability")
			}
			if capabilities[capability] {
				return nil, fmt.Errorf("duplicate model capability")
			}
			capabilities[capability] = true
		}
		if model.Thinking != nil && *model.Thinking && !capabilities["chat"] && !capabilities["image2text"] {
			return nil, fmt.Errorf("THINK requires chat or vision")
		}
	}
	payload["models"] = models
	path := fmt.Sprintf("/providers/%s/instances/%s/models", url.PathEscape(payload["provider_name"].(string)), url.PathEscape(payload["instance_name"].(string)))
	resp, err := c.HTTPClient.Request("POST", path, "api", nil, payload)
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
	for _, field := range []string{"provider_name", "instance_name"} {
		value, ok := cmd.Params[field].(string)
		if !ok || strings.TrimSpace(value) == "" {
			return nil, fmt.Errorf("%s is required", field)
		}
		values[field] = value
	}
	names, ok := cmd.Params["model_names"].([]string)
	if !ok || len(names) == 0 || len(names) > 100 {
		return nil, fmt.Errorf("between 1 and 100 models required")
	}
	seen := make(map[string]bool, len(names))
	for _, name := range names {
		if strings.TrimSpace(name) == "" || seen[name] {
			return nil, fmt.Errorf("invalid or duplicate model name")
		}
		seen[name] = true
	}
	path := fmt.Sprintf("/providers/%s/instances/%s/models", url.PathEscape(values["provider_name"]), url.PathEscape(values["instance_name"]))
	resp, err := c.HTTPClient.Request("DELETE", path, "api", nil, map[string]interface{}{"models": names})
	if err != nil {
		return nil, err
	}
	return modelMutationResponse(resp.StatusCode, resp.Body, "drop instance model")
}

func modelMutationResponse(status int, body []byte, operation string) (ResponseIf, error) {
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
