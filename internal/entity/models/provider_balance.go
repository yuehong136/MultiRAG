package models

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"strconv"
	"strings"
)

// decodeBalanceHTTP keeps credentials and cancellation local to one account query.
func decodeBalanceHTTP(client *http.Client, baseURLs map[string]string, suffix string, api *APIConfig, result interface{}) error {
	if api == nil || api.APIKey == nil || strings.TrimSpace(*api.APIKey) == "" {
		return fmt.Errorf("balance API key is required")
	}
	if suffix == "" {
		return fmt.Errorf("balance endpoint is not configured")
	}
	baseURL, err := resolveModelBaseURL(baseURLs, api.Region)
	if err != nil {
		return err
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	req, err := http.NewRequestWithContext(requestContext(local), http.MethodGet, joinModelURL(baseURL, suffix), nil)
	if err != nil {
		return fmt.Errorf("create balance request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*api.APIKey))
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("balance request: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("balance request failed (HTTP %d)", resp.StatusCode)
	}
	if err := json.NewDecoder(resp.Body).Decode(result); err != nil {
		return fmt.Errorf("decode balance: %w", err)
	}
	return requestContext(local).Err()
}

func parseBalanceAmount(raw string) (float64, error) {
	amount, err := strconv.ParseFloat(raw, 64)
	if err != nil || math.IsNaN(amount) || math.IsInf(amount, 0) {
		return 0, fmt.Errorf("balance amount must be a finite number")
	}
	return amount, nil
}
