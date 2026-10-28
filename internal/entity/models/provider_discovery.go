package models

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
)

// listProviderModels preserves the provider's model IDs and optional owner suffix.
// It does not infer discovery from the bundled catalog.
func listProviderModels(client *http.Client, urls map[string]string, suffix string, api *APIConfig, owner bool) ([]string, error) {
	if suffix == "" {
		return nil, fmt.Errorf("model discovery endpoint is missing")
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	base, err := resolveModelBaseURL(urls, local.Region)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(requestContext(local), http.MethodGet, joinModelURL(base, suffix), nil)
	if err != nil {
		return nil, err
	}
	if local.APIKey != nil && strings.TrimSpace(*local.APIKey) != "" {
		req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*local.APIKey))
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("model discovery: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("model discovery failed (HTTP %d)", resp.StatusCode)
	}
	var result struct {
		Data *[]struct {
			ID      string `json:"id"`
			OwnedBy string `json:"owned_by"`
		} `json:"data"`
		Error any `json:"error"`
	}
	if err := decodeBoundedModelJSON(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("decode model discovery: %w", err)
	}
	if result.Error != nil || result.Data == nil {
		return nil, fmt.Errorf("model discovery returned an error or missing data")
	}
	names := make([]string, 0, len(*result.Data))
	seen := make(map[string]bool, len(*result.Data))
	for _, item := range *result.Data {
		if item.ID == "" || strings.TrimSpace(item.ID) != item.ID || seen[item.ID] {
			return nil, fmt.Errorf("model discovery returned an invalid or duplicate ID")
		}
		seen[item.ID] = true
		name := item.ID
		if owner && item.OwnedBy != "" {
			name += "@" + item.OwnedBy
		}
		names = append(names, name)
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return names, nil
}

// Read one bounded JSON value so oversized whitespace cannot hide trailing data.
func decodeBoundedModelJSON(reader io.Reader, target any) error {
	raw, err := io.ReadAll(io.LimitReader(reader, MaxMediaBytes+1))
	if err != nil {
		return err
	}
	if len(raw) > MaxMediaBytes {
		return fmt.Errorf("model response exceeds 20 MiB")
	}
	return decodeCoHereJSON(bytes.NewReader(raw), target)
}

func strictProviderModelNames(reader io.Reader) ([]string, error) {
	var response struct {
		Data *[]struct {
			ID string `json:"id"`
		} `json:"data"`
		Error json.RawMessage `json:"error"`
	}
	if err := decodeBoundedModelJSON(reader, &response); err != nil || response.Data == nil {
		return nil, fmt.Errorf("invalid or missing provider model list")
	}
	if len(response.Error) > 0 && string(response.Error) != "null" {
		return nil, fmt.Errorf("provider model list failed")
	}
	names, seen := make([]string, 0, len(*response.Data)), map[string]bool{}
	for _, item := range *response.Data {
		if item.ID == "" || strings.TrimSpace(item.ID) != item.ID || seen[item.ID] {
			return nil, fmt.Errorf("invalid or duplicate provider model ID")
		}
		seen[item.ID] = true
		names = append(names, item.ID)
	}
	return names, nil
}
