package cli

import (
	"encoding/json"
	"fmt"
	"net/url"
	"strings"
)

func (c *MultiRAGClient) variableRows(path string) (ResponseIf, error) {
	if c.ServerType != "admin" {
		return nil, fmt.Errorf("this command is only allowed in ADMIN mode")
	}
	response, err := c.HTTPClient.RequestBounded("GET", path, "admin", nil, nil, 4*1024*1024)
	if err != nil {
		return nil, err
	}
	if err := checkAuthResponse("read variables", response); err != nil {
		return nil, err
	}
	var result CommonResponse
	if err := json.Unmarshal(response.Body, &result); err != nil || result.Data == nil {
		return nil, fmt.Errorf("invalid variable list response")
	}
	return &result, nil
}

// ListVariables lists sorted system settings in admin mode.
func (c *MultiRAGClient) ListVariables(cmd *Command) (ResponseIf, error) {
	return c.variableRows("/admin/variables")
}

// ShowVariable selects an exact name, falling back to a literal prefix.
func (c *MultiRAGClient) ShowVariable(cmd *Command) (ResponseIf, error) {
	name, ok := cmd.Params["var_name"].(string)
	if !ok || strings.TrimSpace(name) == "" {
		return nil, fmt.Errorf("var_name not provided")
	}
	return c.variableRows("/admin/variables?" + url.Values{"var_name": {name}}.Encode())
}

// SetVariable persists a typed setting, retaining an explicitly empty string.
func (c *MultiRAGClient) SetVariable(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "admin" {
		return nil, fmt.Errorf("this command is only allowed in ADMIN mode")
	}
	name, ok := cmd.Params["var_name"].(string)
	if !ok || strings.TrimSpace(name) == "" {
		return nil, fmt.Errorf("var_name not provided")
	}
	value, ok := cmd.Params["var_value"].(string)
	if !ok {
		return nil, fmt.Errorf("var_value not provided")
	}
	response, err := c.HTTPClient.RequestBounded("PUT", "/admin/variables", "admin", nil, map[string]any{"var_name": name, "var_value": value}, 4*1024*1024)
	if err != nil {
		return nil, err
	}
	if err := checkAuthResponse("set variable", response); err != nil {
		return nil, err
	}
	var result MessageResponse
	if err := json.Unmarshal(response.Body, &result); err != nil {
		return nil, fmt.Errorf("invalid variable confirmation")
	}
	return &result, nil
}
