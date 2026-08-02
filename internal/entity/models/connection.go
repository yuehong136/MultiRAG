package models

import (
	"fmt"
	"io"
	"net/http"
)

const connectionErrorBodyLimit = 64 * 1024

func checkBearerEndpointConnection(
	client *http.Client,
	baseURLs map[string]string,
	suffix string,
	apiConfig *APIConfig,
	providerName string,
) error {
	if apiConfig == nil || apiConfig.APIKey == nil || *apiConfig.APIKey == "" {
		return fmt.Errorf("API key is required for %s", providerName)
	}
	if suffix == "" {
		return fmt.Errorf("connection endpoint is not configured for %s", providerName)
	}
	baseURL, err := resolveModelBaseURL(baseURLs, apiConfig.Region)
	if err != nil {
		return err
	}
	req, err := http.NewRequest(http.MethodGet, joinModelURL(baseURL, suffix), http.NoBody)
	if err != nil {
		return fmt.Errorf("create %s connection request: %w", providerName, err)
	}
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", *apiConfig.APIKey))

	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("check %s connection: %w", providerName, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusOK {
		return nil
	}
	body, readErr := io.ReadAll(io.LimitReader(resp.Body, connectionErrorBodyLimit))
	if readErr != nil {
		return fmt.Errorf("check %s connection: status %d; read response: %w", providerName, resp.StatusCode, readErr)
	}
	return fmt.Errorf("check %s connection: status %d: %s", providerName, resp.StatusCode, string(body))
}
