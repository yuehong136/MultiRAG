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
	"io"
	"multirag/internal/entity/models"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
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
		return c.HTTPClient.RequestWithIterations("GET", c.pingPath(), "web", nil, nil, iterations)
	}

	// Single ping mode
	resp, err := c.HTTPClient.Request("GET", c.pingPath(), "web", nil, nil)
	if err != nil {
		fmt.Printf("Error: %v\n", err)
		return nil, err
	}

	if pingResponseOK(resp) {
		return &SimpleResponse{Code: 0, Message: "Server is alive"}, nil
	}
	return nil, fmt.Errorf("server is down: HTTP %d", resp.StatusCode)
}

// ShowServerVersion shows the API server version (user mode).
// Aligned with Python /api/v1/system/version (RESTful), so the /api/v1 base.
func (c *MultiRAGClient) ShowServerVersion(cmd *Command) (ResponseIf, error) {
	// Get iterations from command params (for benchmark)
	iterations := 1
	if val, ok := cmd.Params["iterations"].(int); ok && val > 1 {
		iterations = val
	}

	if iterations > 1 {
		// Benchmark mode: multiple iterations
		return c.HTTPClient.RequestWithIterations("GET", "/system/version", "web", nil, nil, iterations)
	}

	// Single mode
	resp, err := c.HTTPClient.Request("GET", "/system/version", "web", nil, nil)
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
		return c.HTTPClient.RequestWithIterations("GET", "/system/configs", "web", nil, nil, iterations)
	}

	// Single mode
	resp, err := c.HTTPClient.Request("GET", "/system/configs", "web", nil, nil)
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

	resp, err := c.HTTPClient.Request("PUT", "/system/log", "web", nil, payload)
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
		return c.HTTPClient.RequestWithIterations("GET", "/datasets", authKind, nil, nil, iterations)
	}

	// Normal mode
	resp, err := c.HTTPClient.Request("GET", "/datasets", authKind, nil, nil)
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
	resp, err := c.HTTPClient.Request("GET", "/datasets", "api", nil, nil)
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
		"dataset_ids":              datasetIDs,
		"question":                 question,
		"similarity_threshold":     0.2,
		"vector_similarity_weight": 0.3,
	}

	if iterations > 1 {
		// Benchmark mode - return raw result for benchmark stats
		return c.HTTPClient.RequestWithIterations("POST", "/datasets/search", "api", nil, payload, iterations)
	}

	// Normal mode
	resp, err := c.HTTPClient.Request("POST", "/datasets/search", "api", nil, payload)
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

	resp, err := c.HTTPClient.Request("POST", "/system/tokens", "web", nil, nil)
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

	resp, err := c.HTTPClient.Request("GET", "/system/tokens", "web", nil, nil)
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

	resp, err := c.HTTPClient.Request("DELETE", fmt.Sprintf("/system/tokens/%s", token), "web", nil, nil)
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

// CreateChunkStore creates a table for a dataset in doc engine
func (c *MultiRAGClient) CreateChunkStore(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("USER mode required")
	}
	payload := map[string]interface{}{}
	dataset, _ := cmd.Params["dataset_name"].(string)
	if strings.TrimSpace(dataset) == "" {
		return nil, fmt.Errorf("dataset required")
	}
	size, ok := cmd.Params["vector_size"].(int)
	if !ok || size <= 0 {
		return nil, fmt.Errorf("positive vector size required")
	}
	payload["vector_size"] = size
	id, err := c.documentDatasetID(dataset)
	if err != nil {
		return nil, err
	}
	payload["kb_id"] = id
	response, err := c.HTTPClient.Request("POST", "/tenant/chunk_store", "api", nil, payload)
	if err != nil {
		return nil, err
	}
	if err := checkAuthResponse("CreateChunkStore", response); err != nil {
		return nil, err
	}
	return &SimpleResponse{Message: "CreateChunkStore succeeded"}, nil
}

func (c *MultiRAGClient) DropChunkStore(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("USER mode required")
	}
	payload := map[string]interface{}{}
	dataset, _ := cmd.Params["dataset_name"].(string)
	if strings.TrimSpace(dataset) == "" {
		return nil, fmt.Errorf("dataset required")
	}
	id, err := c.documentDatasetID(dataset)
	if err != nil {
		return nil, err
	}
	payload["kb_id"] = id
	response, err := c.HTTPClient.Request("DELETE", "/tenant/chunk_store", "api", nil, payload)
	if err != nil {
		return nil, err
	}
	if err := checkAuthResponse("DropChunkStore", response); err != nil {
		return nil, err
	}
	return &SimpleResponse{Message: "DropChunkStore succeeded"}, nil
}

func (c *MultiRAGClient) CreateMetadataStore(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("USER mode required")
	}
	payload := map[string]interface{}{}
	response, err := c.HTTPClient.Request("POST", "/tenant/metadata_store", "api", nil, payload)
	if err != nil {
		return nil, err
	}
	if err := checkAuthResponse("CreateMetadataStore", response); err != nil {
		return nil, err
	}
	return &SimpleResponse{Message: "CreateMetadataStore succeeded"}, nil
}

func (c *MultiRAGClient) DropMetadataStore(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("USER mode required")
	}
	payload := map[string]interface{}{}
	response, err := c.HTTPClient.Request("DELETE", "/tenant/metadata_store", "api", nil, payload)
	if err != nil {
		return nil, err
	}
	if err := checkAuthResponse("DropMetadataStore", response); err != nil {
		return nil, err
	}
	return &SimpleResponse{Message: "DropMetadataStore succeeded"}, nil
}

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

	resp, err := c.HTTPClient.Request("PUT", "/providers", "web", nil, payload)
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

	resp, err := c.HTTPClient.Request("GET", "/providers", "web", nil, nil)
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

	resp, err := c.HTTPClient.Request("DELETE", url, "web", nil, payload)
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

	if c.HTTPClient.APIKey == "" && c.HTTPClient.LoginToken == "" {
		return nil, fmt.Errorf("please login first")
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
		"base_url":      cmd.Params["base_url"],
		"region":        cmd.Params["region"],
	}

	resp, err := c.HTTPClient.Request("POST", url, "api", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to create provider instance: %w", err)
	}

	return modelMutationResponse(resp.StatusCode, resp.Body, "create provider instance")
}

func (c *MultiRAGClient) ListProviderInstances(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	providerName, ok := cmd.Params["provider_name"].(string)
	if !ok {
		return nil, fmt.Errorf("provider name not provided")
	}

	url := fmt.Sprintf("/providers/%s/instances", providerName)

	resp, err := c.HTTPClient.Request("GET", url, "web", nil, nil)
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

	resp, err := c.HTTPClient.Request("GET", url, "web", nil, nil)
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

// ShowInstanceBalance shows balance of a specific instance
// SHOW BALANCE FROM <provider_name> <instance_name>
func (c *MultiRAGClient) ShowInstanceBalance(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}

	if c.HTTPClient.APIKey == "" && c.HTTPClient.LoginToken == "" {
		return nil, fmt.Errorf("user authentication is required")
	}

	instanceName, ok := cmd.Params["instance_name"].(string)
	if !ok {
		return nil, fmt.Errorf("instance name not provided")
	}

	providerName, ok := cmd.Params["provider_name"].(string)
	if !ok {
		return nil, fmt.Errorf("provider name not provided")
	}

	url := fmt.Sprintf("/providers/%s/instances/%s/balance", url.PathEscape(providerName), url.PathEscape(instanceName))

	resp, err := c.HTTPClient.Request("GET", url, "api", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to show balance: %w", err)
	}

	if err := checkAuthResponse("show balance", resp); err != nil {
		return nil, err
	}

	var result CommonDataResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("show balance failed: invalid JSON (%w)", err)
	}

	if result.Data == nil {
		return nil, fmt.Errorf("show balance returned no data")
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

	resp, err := c.HTTPClient.Request("PUT", url, "web", nil, payload)
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

	if c.HTTPClient.APIKey == "" && c.HTTPClient.LoginToken == "" {
		return nil, fmt.Errorf("please login first")
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

	path := fmt.Sprintf("/providers/%s/instances", url.PathEscape(providerName))

	resp, err := c.HTTPClient.Request("DELETE", path, "api", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to drop instance: %w", err)
	}

	return modelMutationResponse(resp.StatusCode, resp.Body, "drop instance")
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

	resp, err := c.HTTPClient.Request("GET", endPoint, "web", nil, nil)
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

	resp, err := c.HTTPClient.Request("PATCH", url, "web", nil, payload)
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
	if compositeModelName, ok := cmd.Params["model_name"].(string); ok && compositeModelName != "" {
		names := strings.Split(compositeModelName, "@")
		if len(names) != 3 {
			return nil, fmt.Errorf("model name must be in format 'model@instance@provider'")
		}
		modelName, instanceName, providerName = names[0], names[1], names[2]
	} else if c.CurrentModel != nil {
		providerName = c.CurrentModel.Provider
		instanceName = c.CurrentModel.Instance
		modelName = c.CurrentModel.Model
	} else {
		return nil, fmt.Errorf("model name not provided and no current model set. Use 'use model' command first")
	}

	message, _ := cmd.Params["message"].(string)
	thinking, _ := cmd.Params["thinking"].(bool)
	stream, _ := cmd.Params["stream"].(bool)
	effort, _ := cmd.Params["effort"].(string)
	verbosity, _ := cmd.Params["verbosity"].(string)
	content, err := chatContent(message, cmd.Params["content_inputs"])
	if err != nil {
		return nil, err
	}
	messages := []models.Message{{Role: "user", Content: content}}
	if err := models.ValidateMessages(messages); err != nil {
		return nil, err
	}
	url := "/chat/completions"
	payload := map[string]interface{}{
		"provider_name": providerName,
		"instance_name": instanceName,
		"model_name":    modelName,
		"messages":      messages,
		"stream":        stream,
		"thinking":      thinking,
	}
	if thinking {
		payload["effort"] = effort
		payload["verbosity"] = verbosity
	}

	if stream {
		startTime := time.Now()
		reader, err := c.HTTPClient.RequestStream("POST", url, "api", nil, payload)
		if err != nil {
			return nil, chatRequestError(err)
		}
		defer reader.Close()

		scanner := bufio.NewScanner(reader)
		scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
		completed := false
		var fullMessage strings.Builder
		reasoningPrint, messagePrint := true, true
		for scanner.Scan() {
			line := scanner.Text()
			if strings.HasPrefix(line, "data:") {
				data := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
				if data == "[DONE]" {
					completed = true
				}
				if strings.HasPrefix(data, "[REASONING]") {
					data = strings.TrimPrefix(data, "[REASONING]")
					if reasoningPrint {
						fmt.Print("Thinking: ")
						reasoningPrint = false
						thinking = true
					}
					fmt.Print(data)
					_ = os.Stdout.Sync()
				}
				if strings.HasPrefix(data, "[MESSAGE]") {
					data = strings.TrimPrefix(data, "[MESSAGE]")
					if messagePrint {
						if thinking {
							fmt.Println()
						}
						fmt.Print("Answer: ")
						messagePrint = false
					}
					fmt.Print(data)
					_ = os.Stdout.Sync()
					fullMessage.WriteString(data)
				}
			} else if strings.HasPrefix(line, "event:") && strings.TrimSpace(strings.TrimPrefix(line, "event:")) == "error" {
				if scanner.Scan() {
					errData := strings.TrimSpace(strings.TrimPrefix(scanner.Text(), "data:"))
					return nil, fmt.Errorf("chat error: %s", errData)
				}
				return nil, fmt.Errorf("chat error: received error event from server")
			}
		}
		duration := time.Since(startTime).Seconds()
		if err := scanner.Err(); err != nil {
			return nil, chatRequestError(err)
		}
		if !completed {
			return nil, chatRequestError(io.ErrUnexpectedEOF)
		}
		fmt.Println()
		return &StreamMessageResponse{Code: 0, Message: fullMessage.String(), Duration: duration}, nil
	}

	resp, err := c.HTTPClient.Request("POST", url, "api", nil, payload)
	if err != nil {
		return nil, chatRequestError(err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("failed to chat model: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}
	var result NonStreamResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("failed to chat model: invalid JSON (%w)", err)
	}
	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}
	return &result, nil
}

func (c *MultiRAGClient) CheckProviderConnection(cmd *Command) (ResponseIf, error) {
	if c.HTTPClient.APIKey == "" && c.HTTPClient.LoginToken == "" {
		return nil, fmt.Errorf("API token not set. Please login first")
	}
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}
	instanceName, ok := cmd.Params["instance_name"].(string)
	if !ok || instanceName == "" {
		return nil, fmt.Errorf("instance name not provided")
	}
	providerName, ok := cmd.Params["provider_name"].(string)
	if !ok || providerName == "" {
		return nil, fmt.Errorf("provider name not provided")
	}

	url := fmt.Sprintf("/providers/%s/instances/%s/connection", providerName, instanceName)
	resp, err := c.HTTPClient.Request(http.MethodGet, url, "api", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to check provider connection: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("failed to check provider connection: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}
	var result SimpleResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("check provider connection failed: invalid JSON (%w)", err)
	}
	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}
	result.Duration = 0
	return &result, nil
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

	names := strings.Split(modelIdentifier, "@")
	if len(names) != 3 {
		return nil, fmt.Errorf("model identifier must be in format 'model@instance@provider'")
	}

	c.CurrentModel = &CurrentModel{
		Provider: names[2],
		Instance: names[1],
		Model:    names[0],
	}

	var result SimpleResponse
	result.Code = 0
	result.Message = fmt.Sprintf("Current model set to: %s@%s@%s", c.CurrentModel.Model, c.CurrentModel.Instance, c.CurrentModel.Provider)
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

// InsertChunksFromFile inserts dataset chunks from a JSON file (user mode, internal)
func (c *MultiRAGClient) InsertChunksFromFile(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("USER mode required")
	}
	payload := map[string]interface{}{}
	file, _ := cmd.Params["file_path"].(string)
	if strings.TrimSpace(file) == "" {
		return nil, fmt.Errorf("file path required")
	}
	payload["file_path"] = file
	response, err := c.HTTPClient.Request("POST", "/tenant/insert_chunks_from_file", "api", nil, payload)
	if err != nil {
		return nil, err
	}
	if err := checkAuthResponse("InsertChunksFromFile", response); err != nil {
		return nil, err
	}
	return &SimpleResponse{Message: "InsertChunksFromFile succeeded"}, nil
}

func (c *MultiRAGClient) InsertMetadataFromFile(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("USER mode required")
	}
	payload := map[string]interface{}{}
	file, _ := cmd.Params["file_path"].(string)
	if strings.TrimSpace(file) == "" {
		return nil, fmt.Errorf("file path required")
	}
	payload["file_path"] = file
	response, err := c.HTTPClient.Request("POST", "/tenant/insert_metadata_from_file", "api", nil, payload)
	if err != nil {
		return nil, err
	}
	if err := checkAuthResponse("InsertMetadataFromFile", response); err != nil {
		return nil, err
	}
	return &SimpleResponse{Message: "InsertMetadataFromFile succeeded"}, nil
}

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
	datasetID, err := c.documentDatasetID(datasetName)
	if err != nil {
		return nil, fmt.Errorf("failed to get dataset ID: %w", err)
	}

	docID, _ := cmd.Params["doc_id"].(string)
	if strings.TrimSpace(docID) == "" {
		return nil, fmt.Errorf("document ID is required")
	}
	getCommand := NewCommand("get_chunk")
	getCommand.Params["chunk_id"], getCommand.Params["doc_id"], getCommand.Params["dataset_name"] = chunkID, docID, datasetID
	if _, err := c.GetChunk(getCommand); err != nil {
		return nil, fmt.Errorf("read chunk before update: %w", err)
	}

	// Parse the JSON body
	var payload map[string]interface{}
	if err := json.Unmarshal([]byte(jsonBody), &payload); err != nil {
		return nil, fmt.Errorf("invalid JSON body: %w", err)
	}
	if payload == nil {
		return nil, fmt.Errorf("chunk update must be a JSON object")
	}

	// Add IDs to payload
	payload["dataset_id"] = datasetID
	payload["document_id"] = docID
	payload["chunk_id"] = chunkID

	resp, err := c.HTTPClient.Request("POST", "/chunk/update", "api", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to update chunk: %w", err)
	}

	if err := checkAuthResponse("update chunk", resp); err != nil {
		return nil, err
	}
	return &SimpleResponse{Message: fmt.Sprintf("Chunk %s updated", chunkID)}, nil
}

// RmTags removes tags from chunks in a dataset
func (c *MultiRAGClient) RmTags(cmd *Command) (ResponseIf, error) {
	return c.removeDatasetTags(cmd)
}

// SetMeta sets (merges) metadata for a document
func (c *MultiRAGClient) SetMeta(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("USER mode required")
	}
	doc, _ := cmd.Params["doc_id"].(string)
	metaJSON, _ := cmd.Params["meta"].(string)
	var meta map[string]interface{}
	if strings.TrimSpace(doc) == "" || json.Unmarshal([]byte(metaJSON), &meta) != nil || meta == nil {
		return nil, fmt.Errorf("document and metadata object are required")
	}
	response, err := c.HTTPClient.Request("POST", "/document/set_meta", "api", nil, map[string]interface{}{"doc_id": doc, "meta": metaJSON})
	if err != nil {
		return nil, err
	}
	if err := checkAuthResponse("set metadata", response); err != nil {
		return nil, err
	}
	var result struct {
		Data *bool `json:"data"`
	}
	if err := json.Unmarshal(response.Body, &result); err != nil || result.Data == nil || !*result.Data {
		return nil, fmt.Errorf("metadata update was not acknowledged")
	}
	return &SimpleResponse{Message: "Metadata updated for " + doc}, nil
}

// RemoveChunks removes chunks from a document
func (c *MultiRAGClient) RemoveChunks(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("USER mode required")
	}
	docID, _ := cmd.Params["doc_id"].(string)
	dataset, _ := cmd.Params["dataset_name"].(string)
	if strings.TrimSpace(docID) == "" || strings.TrimSpace(dataset) == "" {
		return nil, fmt.Errorf("document and dataset required")
	}
	payload := map[string]interface{}{}
	all, _ := cmd.Params["delete_all"].(bool)
	ids, _ := cmd.Params["chunk_ids"].([]string)
	if all == (len(ids) > 0) {
		return nil, fmt.Errorf("provide chunk IDs or ALL")
	}
	seen := map[string]bool{}
	for _, id := range ids {
		if strings.TrimSpace(id) == "" || seen[id] {
			return nil, fmt.Errorf("distinct non-empty chunk IDs required")
		}
		seen[id] = true
	}
	if all {
		payload["delete_all"] = true
	} else {
		payload["chunk_ids"] = ids
	}
	datasetID, err := c.documentDatasetID(dataset)
	if err != nil {
		return nil, err
	}
	path := "/datasets/" + url.PathEscape(datasetID) + "/documents/" + url.PathEscape(docID) + "/chunks"
	resp, err := c.HTTPClient.Request("DELETE", path, "api", nil, payload)
	if err != nil {
		return nil, err
	}
	if err := checkAuthResponse("remove chunks", resp); err != nil {
		return nil, err
	}
	var envelope struct {
		Data *int64 `json:"data"`
	}
	if err := json.Unmarshal(resp.Body, &envelope); err != nil || envelope.Data == nil || *envelope.Data < 0 {
		return nil, fmt.Errorf("invalid chunk deletion count")
	}
	return &SimpleResponse{Message: fmt.Sprintf("%d chunks deleted from %s", *envelope.Data, docID)}, nil
}
