package cli

import (
	"encoding/json"
	"fmt"
	"math"
	"multirag/internal/entity/models"
	"strings"
)

func (c *MultiRAGClient) ModelInference(cmd *Command, rerank bool) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}
	if c.HTTPClient.APIKey == "" && c.HTTPClient.LoginToken == "" {
		return nil, fmt.Errorf("please login first")
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
	path, field, option := "/embeddings", "texts", "dimension"
	if rerank {
		path, field, option = "/rerank", "documents", "top_n"
		query, _ := cmd.Params["query"].(string)
		if strings.TrimSpace(query) == "" {
			return nil, fmt.Errorf("query required")
		}
		payload["query"] = query
	}
	values, ok := cmd.Params[field].([]string)
	if !ok || len(values) == 0 {
		return nil, fmt.Errorf("%s required", field)
	}
	payload[field] = values
	rawNumber, present := cmd.Params[option]
	number, ok := rawNumber.(int)
	if (present && (!ok || number < 0)) || (rerank && !present) {
		return nil, fmt.Errorf("non-negative %s required", option)
	}
	if present {
		payload[option] = number
	}
	resp, err := c.HTTPClient.Request("POST", path, "api", nil, payload)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("model inference: HTTP %d", resp.StatusCode)
	}
	if !rerank {
		var raw struct {
			Code    *int                    `json:"code"`
			Data    *[]models.EmbeddingData `json:"data"`
			Message string                  `json:"message"`
		}
		if err := json.Unmarshal(resp.Body, &raw); err != nil {
			return nil, fmt.Errorf("invalid embedding response")
		}
		if raw.Code == nil || *raw.Code != 0 || raw.Data == nil {
			return nil, fmt.Errorf("embedding failed or missing result")
		}
		vectors, err := models.EmbeddingVectors(*raw.Data, len(values))
		if err != nil {
			return nil, err
		}
		for _, vector := range vectors {
			if number > 0 && len(vector) != number {
				return nil, fmt.Errorf("invalid embedding dimension")
			}
		}
		data := make([]models.EmbeddingData, len(vectors))
		for i, vector := range vectors {
			data[i] = models.EmbeddingData{Index: i, Embedding: vector}
		}
		return &EmbeddingsResponse{Code: *raw.Code, Data: data, Message: raw.Message}, nil
	}
	var raw struct {
		Code    *int                      `json:"code"`
		Data    *[]map[string]interface{} `json:"data"`
		Message string                    `json:"message"`
	}
	if err := json.Unmarshal(resp.Body, &raw); err != nil {
		return nil, fmt.Errorf("invalid model inference response")
	}
	if raw.Code == nil || *raw.Code != 0 || raw.Data == nil {
		return nil, fmt.Errorf("model inference failed or missing result")
	}
	expected := len(values)
	if rerank && number > 0 {
		expected = min(expected, number)
	}
	if len(*raw.Data) != expected {
		return nil, fmt.Errorf("model inference result count mismatch")
	}
	seen := make(map[int]bool, expected)
	for _, row := range *raw.Data {
		index, ok := row["index"].(float64)
		if !ok || index != math.Trunc(index) || index < 0 || index >= float64(len(values)) || seen[int(index)] {
			return nil, fmt.Errorf("invalid model inference index")
		}
		seen[int(index)] = true
		field := "relevance_score"
		value, ok := row[field].(float64)
		if !ok || math.IsNaN(value) || math.IsInf(value, 0) {
			return nil, fmt.Errorf("invalid model inference value")
		}
	}
	return &CommonResponse{Code: *raw.Code, Data: *raw.Data, Message: raw.Message}, nil
}
