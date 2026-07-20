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
	"net/http"
	"os"
	"strings"

	"golang.org/x/term"
)

// LoginUserInteractive performs interactive login with username and password
func (c *MultiRAGClient) LoginUserInteractive(username, password string) error {
	// First, ping the server to check if it's available
	// For admin mode, use /admin/ping with useAPIBase=true
	// For user mode, use /system/ping with useAPIBase=false
	var pingPath string
	var useAPIBase bool
	if c.ServerType == "admin" {
		pingPath = "/admin/ping"
		useAPIBase = true
	} else {
		pingPath = "/system/ping"
		useAPIBase = false
	}

	resp, err := c.HTTPClient.Request("GET", pingPath, useAPIBase, "web", nil, nil)
	if err != nil {
		fmt.Printf("Error: %v\n", err)
		fmt.Println("Can't access server for login (connection failed)")
		return err
	}

	if resp.StatusCode != 200 {
		fmt.Println("Server is down")
		return fmt.Errorf("server is down")
	}

	// Check response - admin returns JSON with message "pong", user returns plain "pong"
	resJSON, err := resp.JSON()
	if err == nil {
		// Admin mode returns {"code":0,"message":"pong"}
		if msg, ok := resJSON["message"].(string); !ok || msg != "pong" {
			fmt.Println("Server is down")
			return fmt.Errorf("server is down")
		}
	} else {
		// User mode returns plain "pong"
		if string(resp.Body) != "pong" {
			fmt.Println("Server is down")
			return fmt.Errorf("server is down")
		}
	}

	// If password is not provided, prompt for it
	if password == "" {
		fmt.Printf("password for %s: ", username)
		var err error
		password, err = readPassword()
		if err != nil {
			return fmt.Errorf("failed to read password: %w", err)
		}
		password = strings.TrimSpace(password)
	}

	// Login
	token, err := c.loginUser(username, password)
	if err != nil {
		fmt.Printf("Error: %v\n", err)
		fmt.Println("Can't access server for login (connection failed)")
		return err
	}

	c.HTTPClient.LoginToken = token
	fmt.Printf("Login user %s successfully\n", username)
	return nil
}

// LoginUser performs user login
func (c *MultiRAGClient) LoginUser(cmd *Command) error {
	// First, ping the server to check if it's available
	// For admin mode, use /admin/ping with useAPIBase=true
	// For user mode, use /system/ping with useAPIBase=false
	var pingPath string
	var useAPIBase bool
	if c.ServerType == "admin" {
		pingPath = "/admin/ping"
		useAPIBase = true
	} else {
		pingPath = "/system/ping"
		useAPIBase = false
	}

	resp, err := c.HTTPClient.Request("GET", pingPath, useAPIBase, "web", nil, nil)
	if err != nil {
		fmt.Printf("Error: %v\n", err)
		fmt.Println("Can't access server for login (connection failed)")
		return err
	}

	if resp.StatusCode != 200 {
		fmt.Println("Server is down")
		return fmt.Errorf("server is down")
	}

	// Check response - admin returns JSON with message "pong", user returns plain "pong"
	resJSON, err := resp.JSON()
	if err == nil {
		// Admin mode returns {"code":0,"message":"pong"}
		if msg, ok := resJSON["message"].(string); !ok || msg != "pong" {
			fmt.Println("Server is down")
			return fmt.Errorf("server is down")
		}
	} else {
		// User mode returns plain "pong"
		if string(resp.Body) != "pong" {
			fmt.Println("Server is down")
			return fmt.Errorf("server is down")
		}
	}

	email, ok := cmd.Params["email"].(string)
	if !ok {
		return fmt.Errorf("email not provided")
	}

	// Get password from user input (hidden)
	var password string
	if c.PasswordPrompt != nil {
		pwd, err := c.PasswordPrompt(fmt.Sprintf("password for %s: ", email))
		if err != nil {
			return fmt.Errorf("failed to read password: %w", err)
		}
		password = pwd
	} else {
		fmt.Printf("password for %s: ", email)
		pwd, err := readPassword()
		if err != nil {
			return fmt.Errorf("failed to read password: %w", err)
		}
		password = pwd
	}
	password = strings.TrimSpace(password)

	// Login
	token, err := c.loginUser(email, password)
	if err != nil {
		fmt.Printf("Error: %v\n", err)
		fmt.Println("Can't access server for login (connection failed)")
		return err
	}

	c.HTTPClient.LoginToken = token
	fmt.Printf("Login user %s successfully\n", email)
	return nil
}

// loginUser performs the actual login request
func (c *MultiRAGClient) loginUser(email, password string) (string, error) {
	// Encrypt password using scrypt (same as Python implementation)
	encryptedPassword, err := EncryptPassword(password)
	if err != nil {
		return "", fmt.Errorf("failed to encrypt password: %w", err)
	}

	payload := map[string]interface{}{
		"email":    email,
		"password": encryptedPassword,
	}

	var path string
	if c.ServerType == "admin" {
		path = "/admin/login"
	} else {
		path = "/user/login"
	}

	resp, err := c.HTTPClient.Request("POST", path, c.ServerType == "admin", "", nil, payload)
	if err != nil {
		return "", err
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return "", fmt.Errorf("login failed: invalid JSON response (%w)", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok || code != 0 {
		msg, _ := resJSON["message"].(string)
		return "", fmt.Errorf("login failed: %s", msg)
	}

	token := resp.Headers.Get("Authorization")
	if token == "" {
		return "", fmt.Errorf("login failed: missing Authorization header")
	}

	return token, nil
}

// Logout ends the current session for both admin and user modes.
func (c *MultiRAGClient) Logout() (ResponseIf, error) {
	if c.HTTPClient.LoginToken == "" {
		return nil, fmt.Errorf("not logged in")
	}

	var path string
	if c.ServerType == "admin" {
		path = "/admin/logout"
	} else {
		path = "/user/logout"
	}

	resp, err := c.HTTPClient.Request("GET", path, c.ServerType == "admin", "web", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("logout failed: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("logout failed: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	resJSON, err := resp.JSON()
	if err != nil {
		return nil, fmt.Errorf("logout failed: invalid JSON response: %w", err)
	}

	code, ok := resJSON["code"].(float64)
	if !ok || code != 0 {
		msg, _ := resJSON["message"].(string)
		return nil, fmt.Errorf("logout failed: %s", msg)
	}

	return &SimpleResponse{Code: 0, Message: "Logout successful"}, nil
}

// ShowCurrentUser shows the current logged-in user information
// TODO: Implement showing current user information when API is available
func (c *MultiRAGClient) ShowCurrentUser(cmd *Command) (ResponseIf, error) {
	// TODO: Call the appropriate API to get current user information
	// Currently there is no /admin/user/info or /user/info API available
	// The /admin/auth API only verifies authorization, does not return user info
	return nil, fmt.Errorf("command 'SHOW CURRENT USER' is not yet implemented")
}

// readPassword reads a password from the terminal without echoing. Callers are
// expected to print their own prompt beforehand. Falls back to plain (visible)
// input when stdin is not a terminal (e.g. piped input).
func readPassword() (string, error) {
	if !term.IsTerminal(int(os.Stdin.Fd())) {
		return readPasswordFallback()
	}

	passwordBytes, err := term.ReadPassword(int(os.Stdin.Fd()))
	fmt.Println() // ReadPassword does not echo the trailing newline
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(passwordBytes)), nil
}

// readPasswordFallback reads password as plain text (fallback mode)
func readPasswordFallback() (string, error) {
	reader := bufio.NewReader(os.Stdin)
	password, err := reader.ReadString('\n')
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(password), nil
}

// ==================== Model provider pool ====================

func (c *MultiRAGClient) ListAvailableProviders(cmd *Command) (ResponseIf, error) {
	endPoint := "/providers?available=true"
	if c.ServerType == "admin" {
		endPoint = "/admin/providers?available=true"
	}

	resp, err := c.HTTPClient.Request("GET", endPoint, true, "web", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to list providers: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to list providers: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	var result CommonResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("failed to list providers: invalid JSON (%w)", err)
	}

	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}
	result.Duration = 0
	return &result, nil
}

func (c *MultiRAGClient) ShowProvider(cmd *Command) (ResponseIf, error) {
	providerName, ok := cmd.Params["provider_name"].(string)
	if !ok {
		return nil, fmt.Errorf("provider_name not provided")
	}

	endPoint := fmt.Sprintf("/providers/%s", providerName)
	if c.ServerType == "admin" {
		endPoint = fmt.Sprintf("/admin/providers/%s", providerName)
	}

	resp, err := c.HTTPClient.Request("GET", endPoint, true, "web", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to show provider: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to show provider: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	var result CommonDataResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("failed to show provider: invalid JSON (%w)", err)
	}

	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}
	result.Duration = 0
	return &result, nil
}

func (c *MultiRAGClient) ListModels(cmd *Command) (ResponseIf, error) {
	providerName, ok := cmd.Params["provider_name"].(string)
	if !ok {
		return nil, fmt.Errorf("provider_name not provided")
	}

	endPoint := fmt.Sprintf("/providers/%s/models", providerName)
	if c.ServerType == "admin" {
		endPoint = fmt.Sprintf("/admin/providers/%s/models", providerName)
	}

	resp, err := c.HTTPClient.Request("GET", endPoint, true, "web", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to list models: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to list models: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	var result CommonResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("failed to list models: invalid JSON (%w)", err)
	}

	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}
	result.Duration = 0
	return &result, nil
}

func (c *MultiRAGClient) ListSupportedModels(cmd *Command) (ResponseIf, error) {
	providerName, ok := cmd.Params["provider_name"].(string)
	if !ok {
		return nil, fmt.Errorf("provider_name not provided")
	}
	instanceName, ok := cmd.Params["instance_name"].(string)
	if !ok {
		return nil, fmt.Errorf("instance_name not provided")
	}

	endpoint := fmt.Sprintf("/providers/%s/instances/%s/models?supported=true", providerName, instanceName)
	if c.ServerType == "admin" {
		endpoint = fmt.Sprintf("/admin/providers/%s/instances/%s/models?supported=true", providerName, instanceName)
	}
	resp, err := c.HTTPClient.Request("GET", endpoint, true, "web", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to list supported models: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("failed to list supported models: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}
	var result CommonResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("failed to list supported models: invalid JSON (%w)", err)
	}
	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}
	return &result, nil
}

func (c *MultiRAGClient) ShowModel(cmd *Command) (ResponseIf, error) {
	providerName, ok := cmd.Params["provider_name"].(string)
	if !ok {
		return nil, fmt.Errorf("provider_name not provided")
	}
	modelName, ok := cmd.Params["model_name"].(string)
	if !ok {
		return nil, fmt.Errorf("model_name not provided")
	}

	endPoint := fmt.Sprintf("/providers/%s/models/%s", providerName, modelName)
	if c.ServerType == "admin" {
		endPoint = fmt.Sprintf("/admin/providers/%s/models/%s", providerName, modelName)
	}

	resp, err := c.HTTPClient.Request("GET", endPoint, true, "web", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to show model: %w", err)
	}

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to show model: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	var result CommonDataResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("failed to show model: invalid JSON (%w)", err)
	}

	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}
	result.Duration = 0
	return &result, nil
}

func (c *MultiRAGClient) SetDefaultModel(cmd *Command) (ResponseIf, error) {
	modelType, ok := cmd.Params["model_type"].(string)
	if !ok {
		return nil, fmt.Errorf("model_type not provided")
	}
	modelProvider, ok := cmd.Params["model_provider"].(string)
	if !ok {
		return nil, fmt.Errorf("model_provider not provided")
	}
	modelInstance, ok := cmd.Params["model_instance"].(string)
	if !ok {
		return nil, fmt.Errorf("model_instance not provided")
	}
	modelName, ok := cmd.Params["model_name"].(string)
	if !ok {
		return nil, fmt.Errorf("model_name not provided")
	}

	payload := map[string]interface{}{
		"model_type":     modelType,
		"model_provider": modelProvider,
		"model_instance": modelInstance,
		"model_name":     modelName,
	}
	resp, err := c.HTTPClient.Request("PATCH", "/models", true, "web", nil, payload)
	if err != nil {
		return nil, fmt.Errorf("failed to set default model: %w", err)
	}
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to set default model: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	var result SimpleResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("failed to set default model: invalid JSON (%w)", err)
	}
	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}
	result.Duration = 0
	return &result, nil
}

func (c *MultiRAGClient) ListDefaultModels(cmd *Command) (ResponseIf, error) {
	resp, err := c.HTTPClient.Request("GET", "/models", true, "web", nil, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to list default models: %w", err)
	}
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("failed to list default models: HTTP %d, body: %s", resp.StatusCode, string(resp.Body))
	}

	var result CommonResponse
	if err = json.Unmarshal(resp.Body, &result); err != nil {
		return nil, fmt.Errorf("failed to list default models: invalid JSON (%w)", err)
	}
	if result.Code != 0 {
		return nil, fmt.Errorf("%s", result.Message)
	}
	result.Duration = 0
	return &result, nil
}
