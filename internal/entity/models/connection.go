package models

import (
	"fmt"
	"net/http"
	"strings"
)

func checkBearerEndpointConnection(
	client *http.Client,
	baseURLs map[string]string,
	suffix string,
	apiConfig *APIConfig,
	providerName string,
) error {
	apiConfig, cancel := modelCallConfig(apiConfig)
	defer cancel()
	if apiConfig == nil || apiConfig.APIKey == nil || strings.TrimSpace(*apiConfig.APIKey) == "" {
		return fmt.Errorf("API key is required for %s", providerName)
	}
	if suffix == "" {
		return fmt.Errorf("connection endpoint is not configured for %s", providerName)
	}
	baseURL, err := resolveModelBaseURL(baseURLs, apiConfig.Region)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(requestContext(apiConfig), http.MethodGet, joinModelURL(baseURL, suffix), http.NoBody)
	if err != nil {
		return fmt.Errorf("create %s connection request: %w", providerName, err)
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*apiConfig.APIKey))

	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("check %s connection: %w", providerName, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusOK {
		return nil
	}
	return fmt.Errorf("check %s connection: status %d", providerName, resp.StatusCode)
}
