package models

import (
	"fmt"
	"maps"
	"net/http"
	"net/url"
	"strings"
	"time"
)

const azureOpenAIAPIVersion = "2024-10-21"

// AzureOpenAIModel uses tenant deployment IDs and an endpoint including /openai.
type AzureOpenAIModel struct {
	*DummyModel
	httpClient        *http.Client
	streamIdleTimeout time.Duration
}

func NewAzureOpenAIModel(urls map[string]string, suffix URLSuffix) *AzureOpenAIModel {
	return &AzureOpenAIModel{
		DummyModel:        NewDummyModel(maps.Clone(urls), suffix),
		httpClient:        &http.Client{Transport: openAIChatTransport, CheckRedirect: rejectModelRedirect},
		streamIdleTimeout: 60 * time.Second,
	}
}

func (a *AzureOpenAIModel) Name() string { return "azure-openai" }

func azureOpenAISuffix(suffix string) string {
	if suffix == "" {
		return ""
	}
	return suffix + "?api-version=" + azureOpenAIAPIVersion
}

func azureOpenAIChatBody(name string, messages []Message, config *ChatConfig, stream bool) (map[string]any, error) {
	if stream && config != nil && config.Stream != nil && !*config.Stream {
		return nil, fmt.Errorf("Azure OpenAI: stream must be true")
	}
	body, err := openAIChatBody(name, messages, config, stream)
	delete(body, "model")
	return body, err
}

// adapter never changes the catalog, caller configuration or another deployment.
func (a *AzureOpenAIModel) adapter(deployment string, api *APIConfig) (*openAIChatDriver, *APIConfig, error) {
	var region *string
	if api != nil {
		region = api.Region
	}
	base, err := resolveModelBaseURL(a.BaseURL, region)
	if err != nil {
		return nil, nil, err
	}
	parsed, err := url.Parse(base)
	if err != nil || parsed.Scheme != "http" && parsed.Scheme != "https" || parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, nil, fmt.Errorf("invalid Azure OpenAI base URL")
	}
	if deployment != "" {
		if strings.TrimSpace(deployment) != deployment || deployment == "." || deployment == ".." {
			return nil, nil, fmt.Errorf("invalid Azure OpenAI deployment")
		}
		base += "/deployments/" + url.PathEscape(deployment)
	}
	local := APIConfig{}
	if api != nil {
		local = *api
	}
	local.Region = nil
	suffix := URLSuffix{Chat: azureOpenAISuffix(a.URLSuffix.Chat), Models: azureOpenAISuffix(a.URLSuffix.Models), Embedding: azureOpenAISuffix(a.URLSuffix.Embedding)}
	driver := newOpenAIChatDriver(map[string]string{"default": base}, suffix, a.Name(), azureOpenAIChatBody)
	driver.httpClient, driver.authHeader = a.httpClient, "api-key"
	driver.streamIdleTimeout, driver.strictDiscovery = a.streamIdleTimeout, true
	driver.strictStream = true
	driver.azureContentFilter, driver.waitForStreamEOF = true, true
	return driver, &local, nil
}

func (a *AzureOpenAIModel) ChatWithMessages(name string, api *APIConfig, messages []Message, config *ChatConfig) (*ChatResponse, error) {
	if strings.TrimSpace(name) == "" {
		return nil, fmt.Errorf("Azure OpenAI deployment is required")
	}
	driver, local, err := a.adapter(name, api)
	if err != nil {
		return nil, err
	}
	return driver.ChatWithMessages(name, local, messages, config)
}

func (a *AzureOpenAIModel) ChatStreamlyWithMessages(name string, messages []Message, api *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if strings.TrimSpace(name) == "" {
		return fmt.Errorf("Azure OpenAI deployment is required")
	}
	driver, local, err := a.adapter(name, api)
	if err != nil {
		return err
	}
	return driver.ChatStreamlyWithMessages(name, messages, local, config, sender)
}

func (a *AzureOpenAIModel) ChatStreamlyWithSender(name, message *string, api *APIConfig, config *ChatConfig, sender func(*string, *string) error) error {
	if name == nil || message == nil {
		return fmt.Errorf("Azure OpenAI deployment and message are required")
	}
	return a.ChatStreamlyWithMessages(*name, []Message{{Role: "user", Content: *message}}, api, config, sender)
}

func (a *AzureOpenAIModel) Embed(name *string, texts []string, api *APIConfig, config *EmbeddingConfig) ([]EmbeddingData, error) {
	if len(texts) == 0 {
		return []EmbeddingData{}, nil
	}
	if name == nil || strings.TrimSpace(*name) == "" {
		return nil, fmt.Errorf("Azure OpenAI deployment is required")
	}
	driver, local, err := a.adapter(*name, api)
	if err != nil {
		return nil, err
	}
	return embedHTTPWithConfig(a.httpClient, driver.BaseURL, driver.URLSuffix.Embedding, name, texts, local, config, embeddingHTTPOptions{authHeader: "api-key", omitModel: true})
}

func (a *AzureOpenAIModel) ListModels(api *APIConfig) ([]string, error) {
	driver, local, err := a.adapter("", api)
	if err != nil {
		return nil, err
	}
	return driver.ListModels(local)
}

func (a *AzureOpenAIModel) CheckConnection(api *APIConfig) error {
	_, err := a.ListModels(api)
	return err
}

var _ ModelDriver = (*AzureOpenAIModel)(nil)
