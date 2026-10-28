package models

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"maps"
	"mime/multipart"
	"net/http"
	"slices"
	"strings"
)

// MinerULocalModel submits document bytes to a tenant's local MinerU service.
type MinerULocalModel struct {
	*DummyModel
	httpClient *http.Client
}

func NewMinerULocalModel(urls map[string]string, suffix URLSuffix) *MinerULocalModel {
	return &MinerULocalModel{NewDummyModel(maps.Clone(urls), suffix), &http.Client{Transport: providerModelTransport}}
}

func (m *MinerULocalModel) Name() string { return "mineru_local" }

func (m *MinerULocalModel) request(method, suffix string, api *APIConfig, body io.Reader, contentType string, result any) (int, error) {
	if suffix == "" {
		return 0, fmt.Errorf("local MinerU endpoint is missing")
	}
	base, err := resolveModelBaseURL(m.BaseURL, api.Region)
	if err != nil {
		return 0, err
	}
	req, err := http.NewRequestWithContext(requestContext(api), method, joinModelURL(base, suffix), body)
	if err != nil {
		return 0, fmt.Errorf("invalid local MinerU endpoint")
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	if api.APIKey != nil && strings.TrimSpace(*api.APIKey) != "" {
		req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*api.APIKey))
	}
	client := *m.httpClient
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	resp, err := client.Do(req)
	if err != nil {
		if err := requestContext(api).Err(); err != nil {
			return 0, err
		}
		return 0, fmt.Errorf("local MinerU request failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusAccepted {
		return 0, fmt.Errorf("local MinerU request failed (HTTP %d)", resp.StatusCode)
	}
	if err := decodeBoundedModelJSON(resp.Body, result); err != nil {
		return 0, fmt.Errorf("invalid local MinerU response")
	}
	return resp.StatusCode, requestContext(api).Err()
}

type mineruLocalResponse struct {
	TaskID string `json:"task_id"`
	Data   *struct {
		TaskID string `json:"task_id"`
	} `json:"data"`
	Status  string                     `json:"status"`
	Code    json.RawMessage            `json:"code"`
	Error   json.RawMessage            `json:"error"`
	Results map[string]json.RawMessage `json:"results"`
}

func (r *mineruLocalResponse) UnmarshalJSON(raw []byte) error {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil || fields == nil {
		return fmt.Errorf("local MinerU response must be an object")
	}
	for _, field := range []string{"task_id", "status", "data", "results"} {
		if value, ok := fields[field]; ok && string(value) == "null" {
			return fmt.Errorf("local MinerU response contains a null field")
		}
	}
	type response mineruLocalResponse
	return json.Unmarshal(raw, (*response)(r))
}

func (r *mineruLocalResponse) validate() error {
	if err := validateModelResponseCode(r.Code); err != nil {
		return fmt.Errorf("local MinerU provider error")
	}
	if len(r.Error) > 0 && string(r.Error) != "null" {
		return fmt.Errorf("local MinerU provider error")
	}
	if r.Data != nil && r.Data.TaskID != "" {
		if r.TaskID != "" && r.TaskID != r.Data.TaskID {
			return fmt.Errorf("conflicting local MinerU task IDs")
		}
		r.TaskID = r.Data.TaskID
	}
	return nil
}

func (m *MinerULocalModel) ParseFile(name *string, content []byte, documentURL string, api *APIConfig) (*ParseTask, error) {
	if documentURL != "" {
		return nil, fmt.Errorf("%w: local MinerU requires file bytes", ErrUnsupportedCapability)
	}
	if err := ValidateMediaBytes(content); err != nil {
		return nil, err
	}
	backend := "pipeline"
	if name != nil {
		if strings.TrimSpace(*name) == "" {
			return nil, fmt.Errorf("local MinerU backend is required")
		}
		backend = *name
	}
	var body bytes.Buffer
	w := multipart.NewWriter(&body)
	part, err := w.CreateFormFile("files", "upload_document.pdf")
	if err != nil {
		return nil, err
	}
	if _, err := part.Write(content); err != nil {
		return nil, err
	}
	if err := w.WriteField("backend", backend); err != nil {
		return nil, err
	}
	if err := w.Close(); err != nil {
		return nil, err
	}
	local, cancel := modelLongOpConfig(api)
	defer cancel()
	var response mineruLocalResponse
	if _, err := m.request(http.MethodPost, m.URLSuffix.DocumentParse, local, &body, w.FormDataContentType(), &response); err != nil {
		return nil, err
	}
	if err := response.validate(); err != nil {
		return nil, err
	}
	if response.Status != "" && response.Status != "pending" && response.Status != "queued" {
		return nil, fmt.Errorf("unexpected local MinerU submission status")
	}
	if len(response.Results) != 0 {
		return nil, fmt.Errorf("unfinished local MinerU task has results")
	}
	task := &ParseTask{TaskID: response.TaskID, Status: "pending"}
	if err := ValidateParseTask(task, true); err != nil {
		return nil, err
	}
	return task, nil
}

func (m *MinerULocalModel) ListTasks(*APIConfig) ([]ParseTask, error) {
	return nil, fmt.Errorf("%w: local MinerU task listing", ErrUnsupportedCapability)
}

func (m *MinerULocalModel) ShowTask(id string, api *APIConfig) (*ParseTask, error) {
	if err := ValidateTaskID(id); err != nil {
		return nil, err
	}
	if m.URLSuffix.Task == "" {
		return nil, fmt.Errorf("local MinerU task endpoint is missing")
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	var response mineruLocalResponse
	status, err := m.request(http.MethodGet, joinModelURL(m.URLSuffix.Task, id+"/result"), local, nil, "", &response)
	if err != nil {
		return nil, err
	}
	if err := response.validate(); err != nil {
		return nil, err
	}
	if response.TaskID != "" && response.TaskID != id {
		return nil, fmt.Errorf("local MinerU task ID mismatch")
	}
	task := &ParseTask{TaskID: id, Status: response.Status}
	if task.Status == "" {
		if status == http.StatusAccepted {
			task.Status = "pending"
		} else {
			task.Status = "success"
		}
	}
	if task.Status == "success" {
		for index, key := range slices.Sorted(maps.Keys(response.Results)) {
			var file struct {
				Markdown *string `json:"md_content"`
			}
			if err := json.Unmarshal(response.Results[key], &file); err != nil || file.Markdown == nil {
				return nil, fmt.Errorf("invalid local MinerU file result")
			}
			task.Segments = append(task.Segments, TaskSegment{Index: index + 1, Content: *file.Markdown})
		}
	} else if len(response.Results) != 0 {
		return nil, fmt.Errorf("unfinished local MinerU task has results")
	}
	if err := ValidateParseTask(task, true); err != nil {
		return nil, err
	}
	return task, nil
}

var _ ModelDriver = (*MinerULocalModel)(nil)
var _ FileParser = (*MinerULocalModel)(nil)
var _ TaskReader = (*MinerULocalModel)(nil)
