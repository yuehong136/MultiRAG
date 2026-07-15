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
	"encoding/json"
	"fmt"
	"net/url"
)

// ListUsers lists all users (admin mode only)
func (c *MultiRAGClient) ListUsers(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "admin" {
		return nil, fmt.Errorf("this command is only allowed in ADMIN mode")
	}

	// Check for benchmark iterations
	iterations := 1
	if val, ok := cmd.Params["iterations"].(int); ok && val > 1 {
		iterations = val
	}

	if iterations > 1 {
		// Benchmark mode - return raw result for benchmark stats
		return c.HTTPClient.RequestWithIterations("GET", "/admin/users", true, "admin", nil, nil, iterations)
	}

	resp, err := c.HTTPClient.Request("GET", "/admin/users", true, "admin", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to list users: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to list users: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok || code != 0 {
		msg, _ := resJSON["message"].(string)
		return nil, fmt.Errorf("failed to list users: %s", msg)
	}

	data, ok := resJSON["data"].([]interface{})
	if !ok {
		return nil, fmt.Errorf("invalid response format")
	}

	// Convert to slice of maps and remove sensitive fields
	tableData := make([]map[string]interface{}, 0, len(data))
	for _, item := range data {
		if itemMap, ok := item.(map[string]interface{}); ok {
			// Remove sensitive fields
			delete(itemMap, "password")
			delete(itemMap, "access_token")
			tableData = append(tableData, itemMap)
		}
	}

	return &CommonResponse{Code: 0, Data: tableData}, nil
}

// ListDatasets lists datasets for a specific user (admin mode)
func (c *MultiRAGClient) ListDatasets(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "admin" {
		return nil, fmt.Errorf("this command is only allowed in ADMIN mode")
	}

	userName, ok := cmd.Params["user_name"].(string)
	if !ok {
		return nil, fmt.Errorf("user_name not provided")
	}

	// Check for benchmark iterations
	iterations := 1
	if val, ok := cmd.Params["iterations"].(int); ok && val > 1 {
		iterations = val
	}

	if iterations > 1 {
		// Benchmark mode - return raw result for benchmark stats
		return c.HTTPClient.RequestWithIterations("GET", fmt.Sprintf("/admin/users/%s/datasets", userName), true, "admin", nil, nil, iterations)
	}

	resp, err := c.HTTPClient.Request("GET", fmt.Sprintf("/admin/users/%s/datasets", userName), true, "admin", nil, nil)
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

	data, ok := resJSON["data"].([]interface{})
	if !ok {
		return nil, fmt.Errorf("invalid response format")
	}

	// Convert to slice of maps and remove avatar
	tableData := make([]map[string]interface{}, 0, len(data))
	for _, item := range data {
		if itemMap, ok := item.(map[string]interface{}); ok {
			delete(itemMap, "avatar")
			tableData = append(tableData, itemMap)
		}
	}

	return &CommonResponse{Code: 0, Data: tableData}, nil
}

// GrantAdmin grants admin privileges to a user (admin mode only)
func (c *MultiRAGClient) GrantAdmin(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "admin" {
		return nil, fmt.Errorf("this command is only allowed in ADMIN mode")
	}

	userName, ok := cmd.Params["user_name"].(string)
	if !ok {
		return nil, fmt.Errorf("user_name not provided")
	}

	resp, err := c.HTTPClient.Request("PUT", fmt.Sprintf("/admin/users/%s/admin", userName), true, "admin", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to grant admin: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to grant admin: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok || code != 0 {
		msg, _ := resJSON["message"].(string)
		return nil, fmt.Errorf("failed to grant admin: %s", msg)
	}

	return &SimpleResponse{Code: 0, Message: fmt.Sprintf("Admin role granted to user: %s", userName)}, nil
}

// RevokeAdmin revokes admin privileges from a user (admin mode only)
func (c *MultiRAGClient) RevokeAdmin(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "admin" {
		return nil, fmt.Errorf("this command is only allowed in ADMIN mode")
	}

	userName, ok := cmd.Params["user_name"].(string)
	if !ok {
		return nil, fmt.Errorf("user_name not provided")
	}

	resp, err := c.HTTPClient.Request("DELETE", fmt.Sprintf("/admin/users/%s/admin", userName), true, "admin", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to revoke admin: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to revoke admin: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok || code != 0 {
		msg, _ := resJSON["message"].(string)
		return nil, fmt.Errorf("failed to revoke admin: %s", msg)
	}

	return &SimpleResponse{Code: 0, Message: fmt.Sprintf("Admin role revoked from user: %s", userName)}, nil
}

// CreateUser creates a new user (admin mode only)
func (c *MultiRAGClient) CreateUser(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "admin" {
		return nil, fmt.Errorf("this command is only allowed in ADMIN mode")
	}

	userName, ok := cmd.Params["user_name"].(string)
	if !ok {
		return nil, fmt.Errorf("user_name not provided")
	}

	password, ok := cmd.Params["password"].(string)
	if !ok {
		return nil, fmt.Errorf("password not provided")
	}

	// Encrypt password using RSA
	encryptedPassword, err := EncryptPassword(password)
	if err != nil {
		return nil, fmt.Errorf("failed to encrypt password: %w", err)
	}

	payload := map[string]interface{}{
		"username": userName,
		"password": encryptedPassword,
		"role":     "user",
	}

	resp, err := c.HTTPClient.Request("POST", "/admin/users", true, "admin", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to create user: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to create user: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok || code != 0 {
		msg, _ := resJSON["message"].(string)
		return nil, fmt.Errorf("failed to create user: %s", msg)
	}

	return &SimpleResponse{Code: 0, Message: fmt.Sprintf("User created successfully: %s", userName)}, nil
}

// ActivateUser activates or deactivates a user (admin mode only)
func (c *MultiRAGClient) ActivateUser(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "admin" {
		return nil, fmt.Errorf("this command is only allowed in ADMIN mode")
	}

	userName, ok := cmd.Params["user_name"].(string)
	if !ok {
		return nil, fmt.Errorf("user_name not provided")
	}

	activateStatus, ok := cmd.Params["activate_status"].(string)
	if !ok {
		return nil, fmt.Errorf("activate_status not provided")
	}

	// Validate activate_status
	if activateStatus != "on" && activateStatus != "off" {
		return nil, fmt.Errorf("activate_status must be 'on' or 'off'")
	}

	payload := map[string]interface{}{
		"activate_status": activateStatus,
	}

	resp, err := c.HTTPClient.Request("PUT", fmt.Sprintf("/admin/users/%s/activate", userName), true, "admin", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to update user activate status: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to update user activate status: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok || code != 0 {
		msg, _ := resJSON["message"].(string)
		return nil, fmt.Errorf("failed to update user activate status: %s", msg)
	}

	return &SimpleResponse{Code: 0, Message: fmt.Sprintf("User '%s' activate status set to '%s'", userName, activateStatus)}, nil
}

// AlterUserPassword changes a user's password (admin mode only)
func (c *MultiRAGClient) AlterUserPassword(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "admin" {
		return nil, fmt.Errorf("this command is only allowed in ADMIN mode")
	}

	userName, ok := cmd.Params["user_name"].(string)
	if !ok {
		return nil, fmt.Errorf("user_name not provided")
	}

	password, ok := cmd.Params["password"].(string)
	if !ok {
		return nil, fmt.Errorf("password not provided")
	}

	// Encrypt password using RSA
	encryptedPassword, err := EncryptPassword(password)
	if err != nil {
		return nil, fmt.Errorf("failed to encrypt password: %w", err)
	}

	payload := map[string]interface{}{
		"new_password": encryptedPassword,
	}

	resp, err := c.HTTPClient.Request("PUT", fmt.Sprintf("/admin/users/%s/password", userName), true, "admin", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to change user password: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to change user password: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok || code != 0 {
		msg, _ := resJSON["message"].(string)
		return nil, fmt.Errorf("failed to change user password: %s", msg)
	}

	return &SimpleResponse{Code: 0, Message: fmt.Sprintf("Password changed for user: %s", userName)}, nil
}

// DropUser deletes a user (admin mode only)
func (c *MultiRAGClient) DropUser(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "admin" {
		return nil, fmt.Errorf("this command is only allowed in ADMIN mode")
	}

	userName, ok := cmd.Params["user_name"].(string)
	if !ok {
		return nil, fmt.Errorf("user_name not provided")
	}

	resp, err := c.HTTPClient.Request("DELETE", fmt.Sprintf("/admin/users/%s", userName), true, "admin", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to delete user: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to delete user: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok || code != 0 {
		msg, _ := resJSON["message"].(string)
		return nil, fmt.Errorf("failed to delete user: %s", msg)
	}

	return &SimpleResponse{Code: 0, Message: fmt.Sprintf("User deleted: %s", userName)}, nil
}

// GenerateAdminToken generates an API token for a user (admin mode only)
func (c *MultiRAGClient) GenerateAdminToken(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "admin" {
		return nil, fmt.Errorf("this command is only allowed in ADMIN mode")
	}

	userName, ok := cmd.Params["user_name"].(string)
	if !ok {
		return nil, fmt.Errorf("user_name not provided")
	}

	resp, err := c.HTTPClient.Request("POST", fmt.Sprintf("/admin/users/%s/keys", userName), true, "admin", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to generate token: %w", err)
	}
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to generate token: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON response: %w", err)
	}
	if code, ok := resJSON["code"].(float64); !ok || code != 0 {
		msg, _ := resJSON["message"].(string)
		return nil, fmt.Errorf("failed to generate token: %s", msg)
	}

	data, _ := resJSON["data"].(map[string]interface{})
	if data == nil {
		data = map[string]interface{}{}
	}
	delete(data, "create_time")
	delete(data, "update_time")
	delete(data, "update_date")
	return &CommonDataResponse{Code: 0, Data: data}, nil
}

// ListAdminTokens lists all API tokens for a user (admin mode only)
func (c *MultiRAGClient) ListAdminTokens(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "admin" {
		return nil, fmt.Errorf("this command is only allowed in ADMIN mode")
	}

	userName, ok := cmd.Params["user_name"].(string)
	if !ok {
		return nil, fmt.Errorf("user_name not provided")
	}

	resp, err := c.HTTPClient.Request("GET", fmt.Sprintf("/admin/users/%s/keys", userName), true, "admin", nil, nil)
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
				delete(m, "dialog_id")
				delete(m, "source")
				delete(m, "create_time")
				delete(m, "update_time")
				delete(m, "update_date")
				tableData = append(tableData, m)
			}
		}
	}
	return &CommonResponse{Code: 0, Data: tableData}, nil
}

// DropAdminToken deletes an API token for a user (admin mode only)
func (c *MultiRAGClient) DropAdminToken(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "admin" {
		return nil, fmt.Errorf("this command is only allowed in ADMIN mode")
	}

	userName, ok := cmd.Params["user_name"].(string)
	if !ok {
		return nil, fmt.Errorf("user_name not provided")
	}

	token, ok := cmd.Params["token"].(string)
	if !ok {
		return nil, fmt.Errorf("token not provided")
	}

	// URL encode the token to handle special characters
	encodedToken := url.QueryEscape(token)

	resp, err := c.HTTPClient.Request("DELETE", fmt.Sprintf("/admin/users/%s/keys/%s", userName, encodedToken), true, "admin", nil, nil)
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

// ShowAdminVersion shows the admin server version (admin mode only).
// Aligned with the admin server /api/v1/admin/version endpoint.
func (c *MultiRAGClient) ShowAdminVersion(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "admin" {
		return nil, fmt.Errorf("this command is only allowed in ADMIN mode")
	}

	// Check for benchmark iterations
	iterations := 1
	if val, ok := cmd.Params["iterations"].(int); ok && val > 1 {
		iterations = val
	}

	if iterations > 1 {
		// Benchmark mode - return raw result for benchmark stats
		return c.HTTPClient.RequestWithIterations("GET", "/admin/version", true, "admin", nil, nil, iterations)
	}

	resp, err := c.HTTPClient.Request("GET", "/admin/version", true, "admin", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to show admin version: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to show admin version: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	var result CommonDataResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("show admin version failed: invalid JSON (%w)", err)
	}

	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}
	result.Duration = 0
	return &result, nil
}
