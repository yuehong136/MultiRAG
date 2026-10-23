package cli

import (
	"encoding/json"
	"fmt"
	"multirag/internal/entity/models"
	"net/url"
	"strings"
)

func (c *MultiRAGClient) ParseFileCommand(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" || c.HTTPClient.APIKey == "" && c.HTTPClient.LoginToken == "" {
		return nil, fmt.Errorf("login in USER mode required")
	}
	name, _ := cmd.Params["composite_model_name"].(string)
	if name == "" && c.CurrentModel != nil {
		name = c.CurrentModel.Model + "@" + c.CurrentModel.Instance + "@" + c.CurrentModel.Provider
	}
	parts := strings.Split(name, "@")
	if len(parts) != 3 {
		return nil, fmt.Errorf("model must be model@instance@provider")
	}
	for _, part := range parts {
		if strings.TrimSpace(part) == "" {
			return nil, fmt.Errorf("empty model identifier")
		}
	}
	payload := map[string]any{"model_name": parts[0], "instance_name": parts[1], "provider_name": parts[2]}
	file, hasFile := cmd.Params["file"].(string)
	documentURL, hasURL := cmd.Params["url"].(string)
	if hasFile == hasURL {
		return nil, fmt.Errorf("provide FILE or URL exclusively")
	}
	if hasFile {
		content, err := readMediaFile(file)
		if err != nil {
			return nil, err
		}
		payload["content"] = content
	} else {
		if err := models.ValidateOCRURL(documentURL); err != nil {
			return nil, err
		}
		payload["url"] = documentURL
	}
	response, err := c.HTTPClient.RequestBounded("POST", "/file/parse", "api", nil, payload, models.MaxMediaBytes+65536)
	if err != nil {
		return nil, err
	}
	return parseTaskResponse(response, "submit parse task", false)
}

func (c *MultiRAGClient) ProviderTaskCommand(cmd *Command, show bool) (ResponseIf, error) {
	if c.ServerType != "user" || c.HTTPClient.APIKey == "" && c.HTTPClient.LoginToken == "" {
		return nil, fmt.Errorf("login in USER mode required")
	}
	provider, _ := cmd.Params["provider_name"].(string)
	instance, _ := cmd.Params["instance_name"].(string)
	if strings.TrimSpace(provider) == "" || strings.TrimSpace(instance) == "" {
		return nil, fmt.Errorf("provider and instance required")
	}
	path := "/providers/" + url.PathEscape(provider) + "/instances/" + url.PathEscape(instance) + "/tasks"
	if show {
		id, _ := cmd.Params["task_id"].(string)
		if err := models.ValidateTaskID(id); err != nil {
			return nil, err
		}
		path += "/" + id
	}
	response, err := c.HTTPClient.RequestBounded("GET", path, "api", nil, nil, models.MaxMediaBytes+65536)
	if err != nil {
		return nil, err
	}
	return parseTaskResponse(response, "read parse task", !show)
}

func parseTaskResponse(response *Response, action string, list bool) (ResponseIf, error) {
	if err := checkAuthResponse(action, response); err != nil {
		return nil, err
	}
	var envelope struct {
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(response.Body, &envelope); err != nil {
		return nil, fmt.Errorf("invalid task response")
	}
	if list {
		var tasks []models.ParseTask
		if err := json.Unmarshal(envelope.Data, &tasks); err != nil || tasks == nil {
			return nil, fmt.Errorf("missing task list")
		}
		rows := make([]map[string]any, 0, len(tasks))
		for _, task := range tasks {
			if err := models.ValidateParseTask(&task, false); err != nil {
				return nil, err
			}
			rows = append(rows, map[string]any{"task_id": task.TaskID, "status": task.Status})
		}
		return &CommonResponse{Data: rows}, nil
	}
	var task models.ParseTask
	if err := json.Unmarshal(envelope.Data, &task); err != nil {
		return nil, fmt.Errorf("invalid task response")
	}
	if err := models.ValidateParseTask(&task, true); err != nil {
		return nil, err
	}
	rows := make([]map[string]any, 0, len(task.Segments)+1)
	for _, segment := range task.Segments {
		rows = append(rows, map[string]any{"task_id": task.TaskID, "status": task.Status, "index": segment.Index, "content": segment.Content})
	}
	if len(rows) == 0 {
		rows = append(rows, map[string]any{"task_id": task.TaskID, "status": task.Status})
	}
	return &CommonResponse{Data: rows}, nil
}
