package models

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"maps"
	"net/http"
	"strings"
)

// MinerUModel submits public document URLs and reads asynchronous parse jobs.
type MinerUModel struct {
	*DummyModel
	httpClient *http.Client
}

func NewMinerUModel(urls map[string]string, suffix URLSuffix) *MinerUModel {
	return &MinerUModel{NewDummyModel(maps.Clone(urls), suffix), &http.Client{Transport: providerModelTransport}}
}

func (m *MinerUModel) Name() string { return "mineru" }

type mineruTaskSubmitResponse struct {
	Code  *int            `json:"code"`
	Error json.RawMessage `json:"error"`
	Data  struct {
		TaskID string `json:"task_id"`
	} `json:"data"`
}

type mineruTaskQueryResponse struct {
	Code  *int            `json:"code"`
	Error json.RawMessage `json:"error"`
	Data  struct {
		TaskID     string `json:"task_id"`
		State      string `json:"state"`
		FullZipURL string `json:"full_zip_url"`
	} `json:"data"`
}

// parseTaskRequest is shared by MinerU-compatible URL task APIs. Provider bodies
// never enter errors, and a missing business code is not a successful response.
func parseTaskRequest(client *http.Client, urls map[string]string, suffix, method string, api *APIConfig, body any, result any) error {
	if api == nil || api.APIKey == nil || strings.TrimSpace(*api.APIKey) == "" {
		return fmt.Errorf("parse API key is required")
	}
	if suffix == "" {
		return fmt.Errorf("parse endpoint is missing")
	}
	base, err := resolveModelBaseURL(urls, api.Region)
	if err != nil {
		return err
	}
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("invalid parse request")
		}
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(requestContext(api), method, joinModelURL(base, suffix), reader)
	if err != nil {
		return fmt.Errorf("invalid parse endpoint")
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*api.APIKey))
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := client.Do(req)
	if err != nil {
		if err := requestContext(api).Err(); err != nil {
			return err
		}
		return fmt.Errorf("parse request failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("parse request failed (HTTP %d)", resp.StatusCode)
	}
	if err := decodeBoundedModelJSON(resp.Body, result); err != nil {
		return fmt.Errorf("invalid parse response")
	}
	return requestContext(api).Err()
}

func submitMinerUTask(client *http.Client, urls map[string]string, suffix string, name *string, content []byte, documentURL string, api *APIConfig) (*ParseTask, error) {
	if name == nil || strings.TrimSpace(*name) == "" {
		return nil, fmt.Errorf("parse model is required")
	}
	if err := ValidateParseInput(content, documentURL); err != nil {
		return nil, err
	}
	if len(content) != 0 {
		return nil, fmt.Errorf("parse API requires a public document URL; direct file upload is unsupported")
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	var response mineruTaskSubmitResponse
	body := map[string]any{"url": documentURL, "model_version": *name}
	if err := parseTaskRequest(client, urls, suffix, http.MethodPost, local, body, &response); err != nil {
		return nil, err
	}
	if response.Code == nil || *response.Code != 0 || len(response.Error) > 0 && string(response.Error) != "null" {
		return nil, fmt.Errorf("parse task submission failed or missing code")
	}
	result := &ParseTask{TaskID: response.Data.TaskID, Status: "pending"}
	if err := ValidateParseTask(result, true); err != nil {
		return nil, err
	}
	return result, nil
}

func readMinerUTask(client *http.Client, urls map[string]string, suffix, id string, api *APIConfig) (*ParseTask, error) {
	if err := ValidateTaskID(id); err != nil {
		return nil, err
	}
	if suffix == "" {
		return nil, fmt.Errorf("parse task endpoint is missing")
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	var response mineruTaskQueryResponse
	if err := parseTaskRequest(client, urls, strings.TrimRight(suffix, "/")+"/"+id, http.MethodGet, local, nil, &response); err != nil {
		return nil, err
	}
	if response.Code == nil || *response.Code != 0 || response.Data.TaskID != id || len(response.Error) > 0 && string(response.Error) != "null" {
		return nil, fmt.Errorf("parse task query failed, missing code, or task ID mismatch")
	}
	result := &ParseTask{TaskID: id}
	switch response.Data.State {
	case "pending":
		result.Status = "pending"
	case "running":
		result.Status = "running"
	case "converting":
		result.Status = "processing"
	case "done":
		if err := ValidateOCRURL(response.Data.FullZipURL); err != nil {
			return nil, fmt.Errorf("parse task has no valid artifact URL")
		}
		result.Status = "success"
		result.Segments = []TaskSegment{{Index: 0, Content: response.Data.FullZipURL}}
	case "failed":
		return nil, fmt.Errorf("parse task failed")
	default:
		return nil, fmt.Errorf("unknown parse task state")
	}
	if err := ValidateParseTask(result, true); err != nil {
		return nil, err
	}
	return result, nil
}

func (m *MinerUModel) ParseFile(name *string, content []byte, documentURL string, api *APIConfig) (*ParseTask, error) {
	return submitMinerUTask(m.httpClient, m.BaseURL, m.URLSuffix.DocumentParse, name, content, documentURL, api)
}

func (m *MinerUModel) ListTasks(*APIConfig) ([]ParseTask, error) {
	return nil, fmt.Errorf("%w: MinerU task listing", ErrUnsupportedCapability)
}

func (m *MinerUModel) ShowTask(id string, api *APIConfig) (*ParseTask, error) {
	return readMinerUTask(m.httpClient, m.BaseURL, m.URLSuffix.DocumentParse, id, api)
}

var _ ModelDriver = (*MinerUModel)(nil)
var _ FileParser = (*MinerUModel)(nil)
var _ TaskReader = (*MinerUModel)(nil)
