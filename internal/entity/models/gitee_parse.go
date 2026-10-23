package models

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"strings"
)

func (g *GiteeModel) taskRequest(method, suffix string, api *APIConfig, typ string, body io.Reader, result any) error {
	if api == nil || api.APIKey == nil || strings.TrimSpace(*api.APIKey) == "" || suffix == "" {
		return fmt.Errorf("gitee task credentials and endpoint required")
	}
	base, err := resolveModelBaseURL(g.BaseURL, api.Region)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(requestContext(api), method, joinModelURL(base, suffix), body)
	if err != nil {
		return fmt.Errorf("invalid task request")
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(*api.APIKey))
	if typ != "" {
		req.Header.Set("Content-Type", typ)
	}
	resp, err := g.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("gitee task request: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK && !(method == http.MethodPost && resp.StatusCode == http.StatusAccepted) {
		return fmt.Errorf("gitee task request failed (HTTP %d)", resp.StatusCode)
	}
	raw, err := readMediaBody(resp.Body, MaxMediaBytes)
	if err != nil {
		return err
	}
	var envelope struct {
		Error json.RawMessage `json:"error"`
		Code  *int            `json:"code"`
	}
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return fmt.Errorf("invalid gitee task response")
	}
	if len(envelope.Error) > 0 && string(envelope.Error) != "null" || envelope.Code != nil && *envelope.Code != 0 {
		return fmt.Errorf("gitee task provider error")
	}
	if err := json.Unmarshal(raw, result); err != nil {
		return fmt.Errorf("invalid gitee task result")
	}
	return requestContext(api).Err()
}

func (g *GiteeModel) ParseFile(name *string, content []byte, documentURL string, api *APIConfig) (*ParseTask, error) {
	if name == nil || strings.TrimSpace(*name) == "" {
		return nil, fmt.Errorf("parse model required")
	}
	if err := ValidateParseInput(content, documentURL); err != nil {
		return nil, err
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	var body bytes.Buffer
	w := multipart.NewWriter(&body)
	if err := w.WriteField("model", *name); err != nil {
		return nil, err
	}
	if documentURL != "" {
		if err := w.WriteField("file", documentURL); err != nil {
			return nil, err
		}
	} else {
		part, err := w.CreateFormFile("file", "document")
		if err != nil {
			return nil, err
		}
		if _, err := part.Write(content); err != nil {
			return nil, err
		}
	}
	if err := w.Close(); err != nil {
		return nil, err
	}
	var task ParseTask
	if err := g.taskRequest(http.MethodPost, g.URLSuffix.DocumentParse, local, w.FormDataContentType(), &body, &task); err != nil {
		return nil, err
	}
	if err := ValidateParseTask(&task, false); err != nil {
		return nil, err
	}
	if task.Status == "success" {
		return g.ShowTask(task.TaskID, local)
	}
	if err := ValidateParseTask(&task, true); err != nil {
		return nil, err
	}
	return &task, nil
}

func (g *GiteeModel) ListTasks(api *APIConfig) ([]ParseTask, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	var result struct {
		Items *[]ParseTask `json:"items"`
	}
	if err := g.taskRequest(http.MethodGet, g.URLSuffix.Tasks, local, "", nil, &result); err != nil {
		return nil, err
	}
	if result.Items == nil {
		return nil, fmt.Errorf("missing task list")
	}
	seen := map[string]bool{}
	for _, task := range *result.Items {
		if err := ValidateParseTask(&task, false); err != nil {
			return nil, err
		}
		if seen[task.TaskID] {
			return nil, fmt.Errorf("duplicate task ID")
		}
		seen[task.TaskID] = true
	}
	return *result.Items, nil
}

func (g *GiteeModel) ShowTask(id string, api *APIConfig) (*ParseTask, error) {
	if err := ValidateTaskID(id); err != nil {
		return nil, err
	}
	if g.URLSuffix.Task == "" {
		return nil, fmt.Errorf("missing task endpoint")
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	var status struct {
		TaskID string `json:"task_id"`
		Status string `json:"status"`
	}
	if err := g.taskRequest(http.MethodGet, g.URLSuffix.Task+"/"+id+"/status", local, "", nil, &status); err != nil {
		return nil, err
	}
	if status.TaskID != "" && status.TaskID != id {
		return nil, fmt.Errorf("task ID mismatch")
	}
	result := &ParseTask{TaskID: id, Status: status.Status}
	if result.Status == "success" {
		var output struct {
			Segments []TaskSegment `json:"segments"`
		}
		if err := g.taskRequest(http.MethodGet, g.URLSuffix.Task+"/"+id+"/get", local, "", nil, &output); err != nil {
			return nil, err
		}
		result.Segments = output.Segments
	}
	if err := ValidateParseTask(result, true); err != nil {
		return nil, err
	}
	return result, nil
}

var _ FileParser = (*GiteeModel)(nil)
var _ TaskReader = (*GiteeModel)(nil)
