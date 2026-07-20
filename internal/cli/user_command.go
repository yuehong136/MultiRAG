//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

package cli

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"strings"
)

// PingServer pings the server to check if it's alive.
// Returns a BenchmarkRawResponse when iterations > 1 (benchmark mode).
func (c *MultiRAGClient) PingServer(cmd *Command) (ResponseIf, error) {
	// Get iterations from command params (for benchmark)
	iterations := 1
	if val, ok := cmd.Params["iterations"].(int); ok && val > 1 {
		iterations = val
	}

	if iterations > 1 {
		// Benchmark mode: multiple iterations
		return c.HTTPClient.RequestWithIterations("GET", "/system/ping", false, "web", nil, nil, iterations)
	}

	// Single ping mode
	resp, err := c.HTTPClient.Request("GET", "/system/ping", false, "web", nil, nil)
	if err != nil {
		fmt.Printf("Error: %v\n", err)
		return nil, err
	}

	if resp.StatusCode == 200 && string(resp.Body) == "pong" {
		return &SimpleResponse{Code: 0, Message: "Server is alive"}, nil
	}
	return nil, fmt.Errorf("server is down: HTTP %d", resp.StatusCode)
}

// ShowServerVersion shows the API server version (user mode).
// Aligned with Python /api/v1/system/version (RESTful), so useAPIBase=true.
func (c *MultiRAGClient) ShowServerVersion(cmd *Command) (ResponseIf, error) {
	// Get iterations from command params (for benchmark)
	iterations := 1
	if val, ok := cmd.Params["iterations"].(int); ok && val > 1 {
		iterations = val
	}

	if iterations > 1 {
		// Benchmark mode: multiple iterations
		return c.HTTPClient.RequestWithIterations("GET", "/system/version", true, "web", nil, nil, iterations)
	}

	// Single mode
	resp, err := c.HTTPClient.Request("GET", "/system/version", true, "web", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to show version: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to show version: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	var result KeyValueResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("show version failed: invalid JSON (%w)", err)
	}
	result.Key = "version"
	result.Duration = 0

	return &result, nil
}

// ListConfigs lists the server configuration as key/value rows (user mode).
func (c *MultiRAGClient) ListConfigs(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	// Get iterations from command params (for benchmark)
	iterations := 1
	if val, ok := cmd.Params["iterations"].(int); ok && val > 1 {
		iterations = val
	}

	if iterations > 1 {
		// Benchmark mode: multiple iterations
		return c.HTTPClient.RequestWithIterations("GET", "/system/configs", true, "web", nil, nil, iterations)
	}

	// Single mode
	resp, err := c.HTTPClient.Request("GET", "/system/configs", true, "web", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to list configs: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to list configs: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	var response CommonDataResponse
	if err = json.Unmarshal(resp.Body, &response); err != nil {
		return nil, fmt.Errorf("list configs failed: invalid JSON (%w)", err)
	}

	var result CommonResponse
	result.Code = 0
	result.Data, err = GetConfigs(&response.Data)
	if err != nil {
		return nil, fmt.Errorf("failed to list configs: %w", err)
	}
	result.Duration = 0
	return &result, nil
}

// GetConfigs flattens the server configuration payload into key/value rows.
// The payload mirrors the Go Config struct, so JSON keys are the exported
// field names (PascalCase) and ports decode as float64.
func GetConfigs(config *map[string]interface{}) ([]map[string]interface{}, error) {
	if config == nil {
		return nil, fmt.Errorf("config is nil")
	}
	result := []map[string]interface{}{}

	result = append(result, map[string]interface{}{
		"key":   "redis_host",
		"value": GetHost(config, "Redis", "Host", "Port"),
	})

	if docEngine, ok := (*config)["DocEngine"].(map[string]interface{}); ok {
		engineType, _ := docEngine["Type"].(string)
		result = append(result, map[string]interface{}{
			"key":   "doc_engine",
			"value": engineType,
		})
		switch engineType {
		case "elasticsearch":
			esCfg, _ := docEngine["ES"].(map[string]interface{})
			esHost, _ := esCfg["Hosts"].(string)
			result = append(result, map[string]interface{}{
				"key":   "elasticsearch_host",
				"value": esHost,
			})
		case "infinity":
			infinityCfg, _ := docEngine["Infinity"].(map[string]interface{})
			infinityHost, _ := infinityCfg["URI"].(string)
			result = append(result, map[string]interface{}{
				"key":   "infinity_host",
				"value": infinityHost,
			})
		case "milvus":
			milvusCfg, _ := docEngine["Milvus"].(map[string]interface{})
			milvusHost, _ := milvusCfg["Hosts"].(string)
			result = append(result, map[string]interface{}{
				"key":   "milvus_host",
				"value": milvusHost,
			})
		default:
			return nil, fmt.Errorf("unknown doc engine: %s", engineType)
		}
	}

	if logConfig, ok := (*config)["Log"].(map[string]interface{}); ok {
		level, _ := logConfig["Level"].(string)
		result = append(result, map[string]interface{}{
			"key":   "log_level",
			"value": level,
		})
	}

	if databaseConfig, ok := (*config)["Database"].(map[string]interface{}); ok {
		driver, _ := databaseConfig["Driver"].(string)
		result = append(result, map[string]interface{}{
			"key":   "database",
			"value": driver,
		})
		result = append(result, map[string]interface{}{
			"key":   "database_host",
			"value": GetHost(config, "Database", "Host", "Port"),
		})
	}

	if language, ok := (*config)["Language"].(string); ok {
		result = append(result, map[string]interface{}{
			"key":   "language",
			"value": language,
		})
	}

	result = append(result, map[string]interface{}{
		"key":   "admin",
		"value": GetHost(config, "Admin", "Host", "Port"),
	})

	if storageEngineConfig, ok := (*config)["StorageEngine"].(map[string]interface{}); ok {
		engineType, _ := storageEngineConfig["Type"].(string)
		result = append(result, map[string]interface{}{
			"key":   "storage_engine",
			"value": engineType,
		})
		switch engineType {
		case "minio":
			minioCfg, _ := storageEngineConfig["Minio"].(map[string]interface{})
			minioHost, _ := minioCfg["Host"].(string)
			result = append(result, map[string]interface{}{
				"key":   "minio_host",
				"value": minioHost,
			})
		case "s3":
			s3Cfg, _ := storageEngineConfig["S3"].(map[string]interface{})
			s3Host, _ := s3Cfg["EndpointURL"].(string)
			result = append(result, map[string]interface{}{
				"key":   "s3_host",
				"value": s3Host,
			})
		case "oss":
			ossCfg, _ := storageEngineConfig["OSS"].(map[string]interface{})
			ossHost, _ := ossCfg["EndpointURL"].(string)
			result = append(result, map[string]interface{}{
				"key":   "oss_host",
				"value": ossHost,
			})
		default:
			return nil, fmt.Errorf("unknown storage engine: %s", engineType)
		}
	}

	return result, nil
}

// GetHost builds a "host:port" string from a nested config section.
// Returns an empty string when the section or its host/port fields are absent.
func GetHost(config *map[string]interface{}, section, hostKey, portKey string) string {
	if config == nil {
		return ""
	}
	if sub, ok := (*config)[section].(map[string]interface{}); ok {
		host, hostOk := sub[hostKey].(string)
		port, portOk := sub[portKey].(float64)
		if hostOk && portOk {
			return fmt.Sprintf("%s:%.0f", host, port)
		}
	}
	return ""
}

// SetLogLevel changes the server log level at runtime (user mode).
func (c *MultiRAGClient) SetLogLevel(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	logLevel, ok := cmd.Params["level"].(string)
	if !ok {
		return nil, fmt.Errorf("no log level")
	}

	payload := map[string]interface{}{
		"level": logLevel,
	}

	resp, err := c.HTTPClient.Request("PUT", "/system/log", true, "web", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to change log level: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to change log level: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	var result SimpleResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("change log level failed: invalid JSON (%w)", err)
	}
	result.Code = 0
	result.Duration = 0
	return &result, nil
}

// ListUserDatasets lists datasets for current user (user mode)
func (c *MultiRAGClient) ListUserDatasets(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	// Check for benchmark iterations
	iterations := 1
	if val, ok := cmd.Params["iterations"].(int); ok && val > 1 {
		iterations = val
	}

	// Use API-token (Bearer) auth when a token is configured, otherwise the web
	// session token. Mirrors ragflow's authKind selection (useAPIToken -> "api").
	authKind := "web"
	if c.HTTPClient.APIKey != "" {
		authKind = "api"
	}

	if iterations > 1 {
		// Benchmark mode - return raw result for benchmark stats
		return c.HTTPClient.RequestWithIterations("GET", "/datasets", true, authKind, nil, nil, iterations)
	}

	// Normal mode
	resp, err := c.HTTPClient.Request("GET", "/datasets", true, authKind, nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to list datasets: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to list datasets: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok || code != 0 {
		msg, _ := resJSON["message"].(string)
		return nil, fmt.Errorf("failed to list datasets: %s", msg)
	}

	data, ok := resJSON["data"].([]interface{})
	if !ok {
		return nil, fmt.Errorf("invalid response format")
	}

	// Convert to slice of maps
	tableData := make([]map[string]interface{}, 0, len(data))
	for _, kb := range data {
		if kbMap, ok := kb.(map[string]interface{}); ok {
			// Remove avatar field
			delete(kbMap, "avatar")
			tableData = append(tableData, kbMap)
		}
	}

	return &CommonResponse{Code: 0, Data: tableData}, nil
}

// getDatasetID gets dataset ID by name
func (c *MultiRAGClient) getDatasetID(datasetName string) (string, error) {
	resp, err := c.HTTPClient.Request("GET", "/datasets", true, "web", nil, nil)
	if err != nil {
		return "", fmt.Errorf("failed to list datasets: %w", err)
	}

	if resp.StatusCode != 200 {
		return "", fmt.Errorf("failed to list datasets: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return "", fmt.Errorf("invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok || code != 0 {
		msg, _ := resJSON["message"].(string)
		return "", fmt.Errorf("failed to list datasets: %s", msg)
	}

	data, ok := resJSON["data"].([]interface{})
	if !ok {
		return "", fmt.Errorf("invalid response format")
	}

	for _, kb := range data {
		if kbMap, ok := kb.(map[string]interface{}); ok {
			if name, _ := kbMap["name"].(string); name == datasetName {
				if id, _ := kbMap["id"].(string); id != "" {
					return id, nil
				}
			}
		}
	}

	return "", fmt.Errorf("dataset '%s' not found", datasetName)
}

// formatEmptyArray converts empty arrays to "[]" string
func formatEmptyArray(v interface{}) string {
	if v == nil {
		return "[]"
	}
	switch val := v.(type) {
	case []interface{}:
		if len(val) == 0 {
			return "[]"
		}
	case []string:
		if len(val) == 0 {
			return "[]"
		}
	case []int:
		if len(val) == 0 {
			return "[]"
		}
	}
	return fmt.Sprintf("%v", v)
}

// SearchOnDatasets searches for chunks in specified datasets (user mode)
func (c *MultiRAGClient) SearchOnDatasets(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	question, ok := cmd.Params["question"].(string)
	if !ok {
		return nil, fmt.Errorf("question not provided")
	}

	datasets, ok := cmd.Params["datasets"].(string)
	if !ok {
		return nil, fmt.Errorf("datasets not provided")
	}

	// Parse dataset names (comma-separated) and convert to IDs
	datasetNames := strings.Split(datasets, ",")
	datasetIDs := make([]string, 0, len(datasetNames))
	for _, name := range datasetNames {
		name = strings.TrimSpace(name)
		id, err := c.getDatasetID(name)
		if err != nil {
			return nil, err
		}
		datasetIDs = append(datasetIDs, id)
	}

	// Check for benchmark iterations
	iterations := 1
	if val, ok := cmd.Params["iterations"].(int); ok && val > 1 {
		iterations = val
	}

	payload := map[string]interface{}{
		"kb_id":                    datasetIDs,
		"question":                 question,
		"similarity_threshold":     0.2,
		"vector_similarity_weight": 0.3,
	}

	if iterations > 1 {
		// Benchmark mode - return raw result for benchmark stats
		return c.HTTPClient.RequestWithIterations("POST", "/chunk/retrieval_test", false, "web", nil, payload, iterations)
	}

	// Normal mode
	resp, err := c.HTTPClient.Request("POST", "/chunk/retrieval_test", false, "web", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to search on datasets: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to search on datasets: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok || code != 0 {
		msg, _ := resJSON["message"].(string)
		return nil, fmt.Errorf("failed to search on datasets: %s", msg)
	}

	data, ok := resJSON["data"].(map[string]interface{})
	if !ok {
		return nil, fmt.Errorf("invalid response format")
	}

	chunks, ok := data["chunks"].([]interface{})
	if !ok {
		return nil, fmt.Errorf("invalid response format: chunks not found")
	}

	// Convert to slice of maps for printing
	tableData := make([]map[string]interface{}, 0, len(chunks))
	for _, chunk := range chunks {
		if chunkMap, ok := chunk.(map[string]interface{}); ok {
			row := map[string]interface{}{
				"id":                chunkMap["chunk_id"],
				"content":           chunkMap["content_with_weight"],
				"document_id":       chunkMap["doc_id"],
				"dataset_id":        chunkMap["kb_id"],
				"docnm_kwd":         chunkMap["docnm_kwd"],
				"image_id":          chunkMap["image_id"],
				"similarity":        chunkMap["similarity"],
				"term_similarity":   chunkMap["term_similarity"],
				"vector_similarity": chunkMap["vector_similarity"],
			}
			// Add optional fields that may be empty arrays
			if v, ok := chunkMap["doc_type_kwd"]; ok {
				row["doc_type_kwd"] = formatEmptyArray(v)
			}
			if v, ok := chunkMap["important_kwd"]; ok {
				row["important_kwd"] = formatEmptyArray(v)
			}
			if v, ok := chunkMap["mom_id"]; ok {
				row["mom_id"] = formatEmptyArray(v)
			}
			if v, ok := chunkMap["positions"]; ok {
				row["positions"] = formatEmptyArray(v)
			}
			if v, ok := chunkMap["content_ltks"]; ok {
				row["content_ltks"] = v
			}
			tableData = append(tableData, row)
		}
	}

	return &CommonResponse{Code: 0, Data: tableData}, nil
}

// CreateToken creates a new API token for the current user (user mode)
func (c *MultiRAGClient) CreateToken(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	resp, err := c.HTTPClient.Request("POST", "/system/tokens", true, "web", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create token: %w", err)
	}
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to create token: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}
	if code, ok := resJSON["code"].(float64); !ok || code != 0 {
		msg, _ := resJSON["message"].(string)
		return nil, fmt.Errorf("failed to create token: %s", msg)
	}

	if data, ok := resJSON["data"].(map[string]interface{}); ok {
		return &CommonDataResponse{Code: 0, Data: data}, nil
	}
	return &SimpleResponse{Code: 0, Message: "Token created successfully"}, nil
}

// ListTokens lists all API tokens for the current user (user mode)
func (c *MultiRAGClient) ListTokens(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	resp, err := c.HTTPClient.Request("GET", "/system/tokens", true, "web", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to list tokens: %w", err)
	}
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to list tokens: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}
	if code, ok := resJSON["code"].(float64); !ok || code != 0 {
		msg, _ := resJSON["message"].(string)
		return nil, fmt.Errorf("failed to list tokens: %s", msg)
	}

	tableData := make([]map[string]interface{}, 0)
	if data, ok := resJSON["data"].([]interface{}); ok {
		for _, item := range data {
			if m, ok := item.(map[string]interface{}); ok {
				tableData = append(tableData, m)
			}
		}
	}
	return &CommonResponse{Code: 0, Data: tableData}, nil
}

// DropToken deletes an API token for the current user (user mode)
func (c *MultiRAGClient) DropToken(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	token, ok := cmd.Params["token"].(string)
	if !ok {
		return nil, fmt.Errorf("token not provided")
	}

	resp, err := c.HTTPClient.Request("DELETE", fmt.Sprintf("/system/tokens/%s", token), true, "web", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to drop token: %w", err)
	}
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to drop token: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}
	if code, ok := resJSON["code"].(float64); !ok || code != 0 {
		msg, _ := resJSON["message"].(string)
		return nil, fmt.Errorf("failed to drop token: %s", msg)
	}

	return &SimpleResponse{Code: 0, Message: "Token dropped successfully"}, nil
}

// CreateDatasetInDocEngine creates a table for a dataset in doc engine
func (c *MultiRAGClient) CreateDatasetInDocEngine(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	datasetName, ok := cmd.Params["dataset_name"].(string)
	if !ok {
		return nil, fmt.Errorf("dataset_name not provided")
	}

	vectorSize, ok := cmd.Params["vector_size"].(int)
	if !ok {
		return nil, fmt.Errorf("vector_size not provided")
	}

	// Get dataset ID by name
	datasetID, err := c.getDatasetID(datasetName)
	if err != nil {
		return nil, err
	}

	payload := map[string]interface{}{
		"kb_id":       datasetID,
		"vector_size": vectorSize,
	}

	resp, err := c.HTTPClient.Request("POST", "/kb/doc_engine_table", false, "web", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to create table: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to create table: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok {
		return nil, fmt.Errorf("invalid response format: code is not a number")
	}

	var result SimpleResponse
	result.Code = int(code)
	if result.Code == 0 {
		result.Message = fmt.Sprintf("Success to create table for dataset: %s", datasetName)
	} else {
		result.Message = fmt.Sprintf("Failed to create table: %v", resJSON)
	}
	result.Duration = 0
	return &result, nil
}

// DropDatasetInDocEngine drops a table for a dataset in doc engine
func (c *MultiRAGClient) DropDatasetInDocEngine(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	datasetName, ok := cmd.Params["dataset_name"].(string)
	if !ok {
		return nil, fmt.Errorf("dataset_name not provided")
	}

	// Get dataset ID by name
	datasetID, err := c.getDatasetID(datasetName)
	if err != nil {
		return nil, err
	}

	payload := map[string]interface{}{
		"kb_id": datasetID,
	}

	resp, err := c.HTTPClient.Request("DELETE", "/kb/doc_engine_table", false, "web", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to drop dataset: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to drop dataset: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok {
		return nil, fmt.Errorf("invalid response format: code is not a number")
	}

	var result SimpleResponse
	result.Code = int(code)
	if result.Code == 0 {
		result.Message = fmt.Sprintf("Success to drop table for dataset: %s", datasetName)
	} else {
		result.Message = fmt.Sprintf("Failed to drop table for dataset: %s: %v", datasetName, resJSON)
	}
	result.Duration = 0
	return &result, nil
}

// CreateMetadataInDocEngine creates the document metadata table for the tenant
func (c *MultiRAGClient) CreateMetadataInDocEngine(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	resp, err := c.HTTPClient.Request("POST", "/tenant/doc_engine_metadata_table", false, "web", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create metadata table: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to create metadata table: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok {
		return nil, fmt.Errorf("invalid response format: code is not a number")
	}

	var result SimpleResponse
	result.Code = int(code)
	if result.Code == 0 {
		result.Message = "Success to create metadata table"
	} else {
		result.Message = fmt.Sprintf("Failed to create metadata table: %v", resJSON)
	}
	result.Duration = 0
	return &result, nil
}

// DropMetadataInDocEngine drops the document metadata table for the tenant
func (c *MultiRAGClient) DropMetadataInDocEngine(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	resp, err := c.HTTPClient.Request("DELETE", "/tenant/doc_engine_metadata_table", false, "web", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to drop metadata table: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to drop metadata table: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok {
		return nil, fmt.Errorf("invalid response format: code is not a number")
	}

	var result SimpleResponse
	result.Code = int(code)
	if result.Code == 0 {
		result.Message = "Success to drop metadata table"
	} else {
		result.Message = fmt.Sprintf("Failed to drop metadata table: %v", resJSON)
	}
	result.Duration = 0
	return &result, nil
}

// ==================== Model provider management ====================

// AddProvider creates a new model provider
// ADD PROVIDER <name>
// ADD PROVIDER <name> <api_key>
func (c *MultiRAGClient) AddProvider(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	providerName, ok := cmd.Params["provider_name"].(string)
	if !ok {
		return nil, fmt.Errorf("provider name not provided")
	}

	// Build payload
	payload := map[string]interface{}{
		"provider_name": providerName,
	}

	resp, err := c.HTTPClient.Request("PUT", "/providers", true, "web", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to add provider: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to add provider: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	var result SimpleResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("add provider failed: invalid JSON (%w)", err)
	}

	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}

	result.Duration = 0
	return &result, nil
}

// ListProviders lists all providers
// LIST PROVIDERS

// ListProviders lists all providers
// LIST PROVIDERS
func (c *MultiRAGClient) ListProviders(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	resp, err := c.HTTPClient.Request("GET", "/providers", true, "web", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to list providers: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to list providers: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	var result CommonResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("list providers failed: invalid JSON (%w)", err)
	}

	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}

	result.Duration = 0
	return &result, nil
}

// DeleteProvider deletes a provider
// DELETE PROVIDER <name>

// DeleteProvider deletes a provider
// DELETE PROVIDER <name>
func (c *MultiRAGClient) DeleteProvider(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	providerName, ok := cmd.Params["provider_name"].(string)
	if !ok {
		return nil, fmt.Errorf("provider name not provided")
	}

	url := fmt.Sprintf("/providers/%s", providerName)

	// Build payload
	payload := map[string]interface{}{
		"llm_factory": providerName,
	}

	resp, err := c.HTTPClient.Request("DELETE", url, true, "web", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to delete provider: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to delete provider: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	var result SimpleResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("delete provider failed: invalid JSON (%w)", err)
	}

	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}

	result.Duration = 0
	return &result, nil
}

// CreateProviderInstance creates a new provider instance
// CREATE PROVIDER <name> INSTANCE <instance_name> <api_key>

// CreateProviderInstance creates a new provider instance
// CREATE PROVIDER <name> INSTANCE <instance_name> <api_key>
func (c *MultiRAGClient) CreateProviderInstance(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	providerName, ok := cmd.Params["provider_name"].(string)
	if !ok {
		return nil, fmt.Errorf("provider name not provided")
	}

	instanceName, ok := cmd.Params["instance_name"].(string)
	if !ok {
		return nil, fmt.Errorf("instance name not provided")
	}

	apiKey, ok := cmd.Params["api_key"].(string)
	if !ok {
		return nil, fmt.Errorf("API key not provided")
	}

	url := fmt.Sprintf("/providers/%s/instances", providerName)

	payload := map[string]interface{}{
		"instance_name": instanceName,
		"api_key":       apiKey,
	}

	resp, err := c.HTTPClient.Request("POST", url, true, "web", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to create provider instance: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to create provider instance: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	var result SimpleResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("create provider instance failed: invalid JSON (%w)", err)
	}

	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}

	result.Duration = 0
	return &result, nil
}

// ListProviderInstances lists all instances of a provider
// LIST INSTANCES FROM PROVIDER <name>

// ListProviderInstances lists all instances of a provider
// LIST INSTANCES FROM PROVIDER <name>
func (c *MultiRAGClient) ListProviderInstances(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	providerName, ok := cmd.Params["provider_name"].(string)
	if !ok {
		return nil, fmt.Errorf("provider name not provided")
	}

	url := fmt.Sprintf("/providers/%s/instances", providerName)

	resp, err := c.HTTPClient.Request("GET", url, true, "web", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to list instances: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to list instances: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	var result CommonResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("list instances failed: invalid JSON (%w)", err)
	}

	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}

	result.Duration = 0
	return &result, nil
}

// ShowProviderInstance shows details of a specific instance
// SHOW INSTANCE <name> FROM PROVIDER <name>

// ShowProviderInstance shows details of a specific instance
// SHOW INSTANCE <name> FROM PROVIDER <name>
func (c *MultiRAGClient) ShowProviderInstance(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	instanceName, ok := cmd.Params["instance_name"].(string)
	if !ok {
		return nil, fmt.Errorf("instance name not provided")
	}

	providerName, ok := cmd.Params["provider_name"].(string)
	if !ok {
		return nil, fmt.Errorf("provider name not provided")
	}

	url := fmt.Sprintf("/providers/%s/instances/%s", providerName, instanceName)

	resp, err := c.HTTPClient.Request("GET", url, true, "web", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to show instance: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to show instance: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	var result CommonDataResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("show instance failed: invalid JSON (%w)", err)
	}

	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}

	result.Duration = 0
	return &result, nil
}

// AlterProviderInstance renames a provider instance
// ALTER INSTANCE <name> NAME <new_name> FROM PROVIDER <name>

// AlterProviderInstance renames a provider instance
// ALTER INSTANCE <name> NAME <new_name> FROM PROVIDER <name>
func (c *MultiRAGClient) AlterProviderInstance(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	instanceName, ok := cmd.Params["instance_name"].(string)
	if !ok {
		return nil, fmt.Errorf("instance name not provided")
	}

	newName, ok := cmd.Params["new_name"].(string)
	if !ok {
		return nil, fmt.Errorf("new name not provided")
	}

	providerName, ok := cmd.Params["provider_name"].(string)
	if !ok {
		return nil, fmt.Errorf("provider name not provided")
	}

	url := fmt.Sprintf("/providers/%s/instances/%s", providerName, instanceName)

	payload := map[string]interface{}{
		"llm_name": newName,
	}

	resp, err := c.HTTPClient.Request("PUT", url, true, "web", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to alter instance: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to alter instance: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	var result SimpleResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("alter instance failed: invalid JSON (%w)", err)
	}

	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}

	result.Duration = 0
	return &result, nil
}

// DropProviderInstance deletes a provider instance
// DROP INSTANCE <name> FROM PROVIDER <name>

// DropProviderInstance deletes a provider instance
// DROP INSTANCE <name> FROM PROVIDER <name>
func (c *MultiRAGClient) DropProviderInstance(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	instanceName, ok := cmd.Params["instance_name"].(string)
	if !ok {
		return nil, fmt.Errorf("instance name not provided")
	}

	providerName, ok := cmd.Params["provider_name"].(string)
	if !ok {
		return nil, fmt.Errorf("provider name not provided")
	}

	payload := map[string]interface{}{
		"instances": []string{instanceName},
	}

	url := fmt.Sprintf("/providers/%s/instances", providerName)

	resp, err := c.HTTPClient.Request("DELETE", url, true, "web", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to drop instance: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to drop instance: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	var result SimpleResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("drop instance failed: invalid JSON (%w)", err)
	}

	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}

	result.Duration = 0
	return &result, nil
}

func (c *MultiRAGClient) ListInstanceModels(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}
	providerName, ok := cmd.Params["provider_name"].(string)
	if !ok {
		return nil, fmt.Errorf("provider_name not provided")
	}
	instanceName, ok := cmd.Params["instance_name"].(string)
	if !ok {
		return nil, fmt.Errorf("instance_name not provided")
	}

	var endPoint string
	endPoint = fmt.Sprintf("/providers/%s/instances/%s/models", providerName, instanceName)

	resp, err := c.HTTPClient.Request("GET", endPoint, true, "web", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to list instance models: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to list instance models: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	var result CommonResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("failed to list instance models: invalid JSON (%w)", err)
	}

	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}
	result.Duration = 0
	return &result, nil
}

func (c *MultiRAGClient) EnableOrDisableModel(cmd *Command, status string) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	modelName, ok := cmd.Params["model_name"].(string)
	if !ok {
		return nil, fmt.Errorf("model name not provided")
	}

	instanceName, ok := cmd.Params["instance_name"].(string)
	if !ok {
		return nil, fmt.Errorf("instance name not provided")
	}

	providerName, ok := cmd.Params["provider_name"].(string)
	if !ok {
		return nil, fmt.Errorf("provider name not provided")
	}

	url := fmt.Sprintf("/providers/%s/instances/%s/models/%s", providerName, instanceName, modelName)

	payload := map[string]interface{}{
		"status": status,
	}

	resp, err := c.HTTPClient.Request("PATCH", url, true, "web", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to enable/disable model: %w", err)
	}
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to enable/disable model: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}
	var result SimpleResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("enable/disable model failed: invalid JSON (%w)", err)
	}
	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}
	result.Duration = 0
	return &result, nil
}

func (c *MultiRAGClient) ChatToModel(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	var providerName, instanceName, modelName string

	// Check if model_name is provided in command
	if compositeModelName, ok := cmd.Params["model_name"].(string); ok && compositeModelName != "" {
		names := strings.Split(compositeModelName, "/")
		if len(names) != 3 {
			return nil, fmt.Errorf("model name must be in format 'provider/instance/model'")
		}
		providerName = names[0]
		instanceName = names[1]
		modelName = names[2]
	} else if c.CurrentModel != nil {
		// Use current model if set
		providerName = c.CurrentModel.Provider
		instanceName = c.CurrentModel.Instance
		modelName = c.CurrentModel.Model
	} else {
		return nil, fmt.Errorf("model name not provided and no current model set. Use 'use model' command first")
	}

	message := cmd.Params["message"].(string)
	reasoning, _ := cmd.Params["reasoning"].(bool)

	url := fmt.Sprintf("/providers/%s/instances/%s/models/%s", providerName, instanceName, modelName)

	payload := map[string]interface{}{
		"message":   message,
		"stream":    true, // use stream API
		"reasoning": reasoning,
	}

	// Call stream http api
	reader, duration, err := c.HTTPClient.RequestStream("POST", url, true, "web", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to chat model: %w", err)
	}
	defer reader.Close()

	// Parse SSE and output to console
	scanner := bufio.NewScanner(reader)
	var fullMessage strings.Builder

	reasoningPrint := true
	messagePrint := true
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(line, "data:") {
			data := strings.TrimPrefix(line, "data:")
			data = strings.TrimSpace(data)

			if strings.HasPrefix(data, "[REASONING]") {
				data = strings.TrimPrefix(data, "[REASONING]")
				if reasoningPrint {
					fmt.Print("Thinking: ")
					reasoningPrint = false
				} else {
					fmt.Print(data)
				}
				os.Stdout.Sync()
			}
			if strings.HasPrefix(data, "[MESSAGE]") {
				data = strings.TrimPrefix(data, "[MESSAGE]")
				if messagePrint {
					if reasoning {
						fmt.Println()
					}
					fmt.Print("Answer: ")
					messagePrint = false
				} else {
					fmt.Print(data)
					os.Stdout.Sync()
					fullMessage.WriteString(data)
				}
			}
		} else if strings.HasPrefix(line, "event:error") {
			// error event
			if scanner.Scan() {
				errData := strings.TrimPrefix(scanner.Text(), "data:")
				errData = strings.TrimSpace(errData)
				return nil, fmt.Errorf("chat error: %s", errData)
			}
			// If there's an error, return a generic error
			return nil, fmt.Errorf("chat error: received error event from server")
		}
	}

	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("error reading stream: %w", err)
	}

	fmt.Println()

	result := &StreamMessageResponse{
		Code:     0,
		Message:  fullMessage.String(),
		Duration: duration,
	}
	return result, nil
}

// UseModel sets the current model for chat

// UseModel sets the current model for chat
func (c *MultiRAGClient) UseModel(cmd *Command) (ResponseIf, error) {
	if c.HTTPClient.APIKey == "" && c.HTTPClient.LoginToken == "" {
		return nil, fmt.Errorf("API token not set. Please login first")
	}
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	modelIdentifier, ok := cmd.Params["model_identifier"].(string)
	if !ok || modelIdentifier == "" {
		return nil, fmt.Errorf("model identifier not provided")
	}

	names := strings.Split(modelIdentifier, "/")
	if len(names) != 3 {
		return nil, fmt.Errorf("model identifier must be in format 'provider/instance/model'")
	}

	c.CurrentModel = &CurrentModel{
		Provider: names[0],
		Instance: names[1],
		Model:    names[2],
	}

	var result SimpleResponse
	result.Code = 0
	result.Message = fmt.Sprintf("Current model set to: %s/%s/%s", c.CurrentModel.Provider, c.CurrentModel.Instance, c.CurrentModel.Model)
	return &result, nil
}

// ShowCurrentModel displays the current model configuration

// ShowCurrentModel displays the current model configuration
func (c *MultiRAGClient) ShowCurrentModel(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	if c.CurrentModel == nil {
		return nil, fmt.Errorf("no current model set. Use 'use model' command first")
	}

	var result CommonResponse
	result.Code = 0
	result.Data = []map[string]interface{}{
		{
			"provider": c.CurrentModel.Provider,
			"instance": c.CurrentModel.Instance,
			"model":    c.CurrentModel.Model,
		},
	}
	return &result, nil
}

// Context related commands

// CEList handles the ls command - lists nodes using Context Engine

// InsertDatasetFromFile inserts dataset chunks from a JSON file (user mode, internal)
func (c *MultiRAGClient) InsertDatasetFromFile(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	filePath, ok := cmd.Params["file_path"].(string)
	if !ok {
		return nil, fmt.Errorf("file_path not provided")
	}

	payload := map[string]interface{}{
		"file_path": filePath,
	}

	resp, err := c.HTTPClient.Request("POST", "/kb/insert_from_file", false, "web", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to insert dataset from file: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to insert dataset from file: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok {
		return nil, fmt.Errorf("invalid response format: code is not a number")
	}

	var result SimpleResponse
	result.Code = int(code)
	if result.Code == 0 {
		result.Message = fmt.Sprintf("Success to insert dataset from file: %s", filePath)
	} else {
		result.Message = fmt.Sprintf("Failed to insert dataset from file: %v", resJSON)
	}
	result.Duration = 0
	return &result, nil
}

// InsertMetadataFromFile inserts metadata from a JSON file (user mode, internal)
func (c *MultiRAGClient) InsertMetadataFromFile(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	filePath, ok := cmd.Params["file_path"].(string)
	if !ok {
		return nil, fmt.Errorf("file_path not provided")
	}

	payload := map[string]interface{}{
		"file_path": filePath,
	}

	resp, err := c.HTTPClient.Request("POST", "/tenant/insert_metadata_from_file", false, "web", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to insert metadata from file: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to insert metadata from file: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok {
		return nil, fmt.Errorf("invalid response format: code is not a number")
	}

	var result SimpleResponse
	result.Code = int(code)
	if result.Code == 0 {
		result.Message = fmt.Sprintf("Success to insert metadata from file: %s", filePath)
	} else {
		result.Message = fmt.Sprintf("Failed to insert metadata from file: %v", resJSON)
	}
	result.Duration = 0
	return &result, nil
}

// UpdateChunk updates a chunk in a dataset
func (c *MultiRAGClient) UpdateChunk(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	chunkID, ok := cmd.Params["chunk_id"].(string)
	if !ok {
		return nil, fmt.Errorf("chunk_id not provided")
	}

	datasetName, ok := cmd.Params["dataset_name"].(string)
	if !ok {
		return nil, fmt.Errorf("dataset_name not provided")
	}

	jsonBody, ok := cmd.Params["json_body"].(string)
	if !ok {
		return nil, fmt.Errorf("json_body not provided")
	}

	// Look up dataset_id from dataset_name
	datasetID, err := c.getDatasetID(datasetName)
	if err != nil {
		return nil, fmt.Errorf("failed to get dataset ID: %w", err)
	}

	// Try to get doc_id from the chunk get endpoint
	getResp, err := c.HTTPClient.Request("GET", "/chunk/get?chunk_id="+chunkID, false, "web", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to get chunk info: %w", err)
	}

	var docID string
	if getResp.StatusCode == 200 {
		getJSON, err := getResp.JSON()
		if err == nil {
			if data, ok := getJSON["data"].(map[string]interface{}); ok {
				if d, ok := data["doc_id"].(string); ok {
					docID = d
				}
			}
		}
	}

	if docID == "" {
		return nil, fmt.Errorf("could not find document_id for chunk %s. Please provide document_id explicitly", chunkID)
	}

	// Parse the JSON body
	var payload map[string]interface{}
	if err := json.Unmarshal([]byte(jsonBody), &payload); err != nil {
		return nil, fmt.Errorf("invalid JSON body: %w", err)
	}

	// Add IDs to payload
	payload["dataset_id"] = datasetID
	payload["document_id"] = docID
	payload["chunk_id"] = chunkID

	resp, err := c.HTTPClient.Request("POST", "/chunk/update", false, "web", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to update chunk: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to update chunk: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok {
		return nil, fmt.Errorf("invalid response format: code is not a number")
	}

	var result SimpleResponse
	result.Code = int(code)
	if result.Code == 0 {
		result.Message = fmt.Sprintf("Success to update chunk: %s", chunkID)
	} else {
		result.Message = fmt.Sprintf("Failed to update chunk: %v", resJSON)
	}
	result.Duration = 0
	return &result, nil
}

// RmTags removes tags from chunks in a dataset
func (c *MultiRAGClient) RmTags(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	datasetName, ok := cmd.Params["dataset_name"].(string)
	if !ok {
		return nil, fmt.Errorf("dataset_name not provided")
	}

	kbID, err := c.getDatasetID(datasetName)
	if err != nil {
		return nil, err
	}

	tags, ok := cmd.Params["tags"].([]string)
	if !ok {
		return nil, fmt.Errorf("tags not provided")
	}

	payload := map[string]interface{}{
		"tags": tags,
	}

	resp, err := c.HTTPClient.Request("POST", "/kb/"+kbID+"/rm_tags", false, "web", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to remove tags: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to remove tags: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok {
		return nil, fmt.Errorf("invalid response format: code is not a number")
	}

	var result SimpleResponse
	result.Code = int(code)
	if result.Code == 0 {
		result.Message = fmt.Sprintf("Success to remove tags from dataset: %s", kbID)
	} else {
		result.Message = fmt.Sprintf("Failed to remove tags: %v", resJSON)
	}
	result.Duration = 0
	return &result, nil
}

// SetMeta sets (merges) metadata for a document
func (c *MultiRAGClient) SetMeta(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	docID, ok := cmd.Params["doc_id"].(string)
	if !ok {
		return nil, fmt.Errorf("doc_id not provided")
	}

	metaJSON, ok := cmd.Params["meta"].(string)
	if !ok {
		return nil, fmt.Errorf("meta not provided")
	}

	payload := map[string]interface{}{
		"doc_id": docID,
		"meta":   metaJSON,
	}

	resp, err := c.HTTPClient.Request("POST", "/document/set_meta", false, "web", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to set metadata: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to set metadata: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok {
		return nil, fmt.Errorf("invalid response format: code is not a number")
	}

	var result SimpleResponse
	result.Code = int(code)
	if result.Code == 0 {
		result.Message = fmt.Sprintf("Success to set metadata for document: %s", docID)
	} else {
		result.Message = fmt.Sprintf("Failed to set metadata: %v", resJSON)
	}
	result.Duration = 0
	return &result, nil
}

// RemoveChunks removes chunks from a document
func (c *MultiRAGClient) RemoveChunks(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	docID, ok := cmd.Params["doc_id"].(string)
	if !ok {
		return nil, fmt.Errorf("doc_id not provided")
	}

	payload := map[string]interface{}{
		"doc_id": docID,
	}

	// Check if delete_all is set
	if deleteAll, ok := cmd.Params["delete_all"].(bool); ok && deleteAll {
		payload["delete_all"] = true
	} else if chunkIDs, ok := cmd.Params["chunk_ids"].([]string); ok {
		payload["chunk_ids"] = chunkIDs
	}

	resp, err := c.HTTPClient.Request("POST", "/chunk/rm", false, "web", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to remove chunks: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to remove chunks: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok {
		return nil, fmt.Errorf("invalid response format: code is not a number")
	}

	var result SimpleResponse
	result.Code = int(code)
	if result.Code == 0 {
		deletedCount := int64(0)
		switch data := resJSON["data"].(type) {
		case float64:
			deletedCount = int64(data)
		case map[string]interface{}:
			if count, ok := data["deleted_count"].(float64); ok {
				deletedCount = int64(count)
			}
		}
		result.Message = fmt.Sprintf("Success to remove chunks from document %s: %d chunks deleted", docID, deletedCount)
	} else {
		result.Message = fmt.Sprintf("Failed to remove chunks: %v", resJSON)
	}
	result.Duration = 0
	return &result, nil
}
