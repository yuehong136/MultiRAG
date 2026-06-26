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
	"context"
	"fmt"

	ce "multirag/internal/cli/contextengine"
)

// PasswordPromptFunc is a function type for password input
type PasswordPromptFunc func(prompt string) (string, error)

// httpClientAdapter adapts *HTTPClient to ce.HTTPClientInterface so the Context
// Engine providers can issue requests through the CLI's HTTP client.
type httpClientAdapter struct {
	client *HTTPClient
}

// Request resolves the "auto" auth kind against the client's available
// credentials (API key vs. login session token) and forwards the call.
func (a *httpClientAdapter) Request(method, path string, useAPIBase bool, authKind string, headers map[string]string, jsonBody map[string]interface{}) (*ce.HTTPResponse, error) {
	if authKind == "auto" || authKind == "" {
		if a.client.APIKey != "" {
			authKind = "api"
		} else {
			authKind = "web"
		}
	}
	resp, err := a.client.Request(method, path, useAPIBase, authKind, headers, jsonBody)
	if err != nil {
		return nil, err
	}
	return &ce.HTTPResponse{
		StatusCode: resp.StatusCode,
		Body:       resp.Body,
		Headers:    resp.Headers,
	}, nil
}

// MultiRAGClient handles API interactions with the MultiRAG server
type MultiRAGClient struct {
	HTTPClient     *HTTPClient
	ServerType     string             // "admin" or "user"
	PasswordPrompt PasswordPromptFunc // Function for password input
	OutputFormat   OutputFormat       // Output format used when rendering CE results
	ContextEngine  *ce.Engine         // Context Engine for the SQL-parser ls/search path
}

// NewMultiRAGClient creates a new MultiRAG client
func NewMultiRAGClient(serverType string) *MultiRAGClient {
	httpClient := NewHTTPClient()
	// Set port from configuration file based on server type
	if serverType == "admin" {
		httpClient.Port = 9381
	} else {
		httpClient.Port = 9380
	}

	client := &MultiRAGClient{
		HTTPClient: httpClient,
		ServerType: serverType,
	}
	client.initContextEngine()
	return client
}

// initContextEngine wires the client-side Context Engine used by the
// SQL-parser ls/search path (CEList/CESearch). The CLI also builds its own
// engine in NewCLIWithArgs for the primary executeContextEngine path.
func (c *MultiRAGClient) initContextEngine() {
	engine := ce.NewEngine()
	engine.RegisterProvider(ce.NewDatasetProvider(&httpClientAdapter{client: c.HTTPClient}))
	engine.RegisterProvider(ce.NewFileProvider(&httpClientAdapter{client: c.HTTPClient}))
	c.ContextEngine = engine
}

// ExecuteCommand dispatches a parsed command to the admin or user executor
// based on the client's server type. Command functions return a ResponseIf
// which the caller renders via PrintOut.
func (c *MultiRAGClient) ExecuteCommand(cmd *Command) (ResponseIf, error) {
	switch c.ServerType {
	case "admin":
		return c.ExecuteAdminCommand(cmd)
	case "user":
		return c.ExecuteUserCommand(cmd)
	default:
		return nil, fmt.Errorf("invalid server type: %s", c.ServerType)
	}
}

// ExecuteAdminCommand executes a command in admin mode.
func (c *MultiRAGClient) ExecuteAdminCommand(cmd *Command) (ResponseIf, error) {
	switch cmd.Type {
	case "login_user":
		return nil, c.LoginUser(cmd)
	case "logout":
		return c.Logout()
	case "ping_server":
		return c.PingServer(cmd)
	case "benchmark":
		return c.RunBenchmark(cmd)
	case "show_current_user":
		return c.ShowCurrentUser(cmd)
	case "list_users":
		return c.ListUsers(cmd)
	case "list_datasets":
		return c.ListDatasets(cmd)
	case "grant_admin":
		return c.GrantAdmin(cmd)
	case "revoke_admin":
		return c.RevokeAdmin(cmd)
	case "create_user":
		return c.CreateUser(cmd)
	case "activate_user":
		return c.ActivateUser(cmd)
	case "alter_user":
		return c.AlterUserPassword(cmd)
	case "drop_user":
		return c.DropUser(cmd)
	case "generate_token":
		return c.GenerateAdminToken(cmd)
	case "list_tokens":
		return c.ListAdminTokens(cmd)
	case "drop_token":
		return c.DropAdminToken(cmd)
	case "list_available_providers":
		return c.ListAvailableProviders(cmd)
	case "show_provider":
		return c.ShowProvider(cmd)
	case "list_provider_models":
		return c.ListModels(cmd)
	case "show_model":
		return c.ShowModel(cmd)
	// TODO: Implement other admin commands
	default:
		return nil, fmt.Errorf("command '%s' would be executed with API", cmd.Type)
	}
}

// ExecuteUserCommand executes a command in user mode.
func (c *MultiRAGClient) ExecuteUserCommand(cmd *Command) (ResponseIf, error) {
	switch cmd.Type {
	case "login_user":
		return nil, c.LoginUser(cmd)
	case "logout":
		return c.Logout()
	case "ping_server":
		return c.PingServer(cmd)
	case "benchmark":
		return c.RunBenchmark(cmd)
	case "show_current_user":
		return c.ShowCurrentUser(cmd)
	case "list_user_datasets":
		return c.ListUserDatasets(cmd)
	case "search_on_datasets":
		return c.SearchOnDatasets(cmd)
	case "create_token":
		return c.CreateToken(cmd)
	case "list_tokens":
		return c.ListTokens(cmd)
	case "drop_token":
		return c.DropToken(cmd)
	case "create_index":
		return c.CreateIndex(cmd)
	case "drop_index":
		return c.DropIndex(cmd)
	case "create_doc_meta_index":
		return c.CreateDocMetaIndex(cmd)
	case "drop_doc_meta_index":
		return c.DropDocMetaIndex(cmd)
	case "list_available_providers":
		return c.ListAvailableProviders(cmd)
	case "show_provider":
		return c.ShowProvider(cmd)
	case "list_provider_models":
		return c.ListModels(cmd)
	case "show_model":
		return c.ShowModel(cmd)
	// Provider commands
	case "create_provider":
		return c.CreateProvider(cmd)
	case "list_providers":
		return c.ListProviders(cmd)
	case "drop_provider":
		return c.DropProvider(cmd)
	// Context Engine commands (SQL-parser path)
	case "ce_ls":
		return c.CEList(cmd)
	case "ce_search":
		return c.CESearch(cmd)
	// TODO: Implement other user commands
	default:
		return nil, fmt.Errorf("command '%s' would be executed with API", cmd.Type)
	}
}

// CEList executes a ce_ls command through the Context Engine and returns a
// CEListResponse. This is the SQL-parser counterpart to cli.go's
// executeContextEngine "ls" branch (which additionally supports -n).
func (c *MultiRAGClient) CEList(cmd *Command) (ResponseIf, error) {
	path, _ := cmd.Params["path"].(string)
	if path == "" {
		path = "datasets"
	}

	opts := &ce.ListOptions{}
	if limit, ok := cmd.Params["limit"].(int); ok {
		opts.Limit = limit
	}

	result, err := c.ContextEngine.List(context.Background(), path, opts)
	if err != nil {
		return nil, err
	}

	return &CEListResponse{
		Code:         0,
		Data:         ce.FormatNodes(result.Nodes, string(c.OutputFormat)),
		outputFormat: c.OutputFormat,
	}, nil
}

// CESearch executes a ce_search command through the Context Engine and returns
// a CESearchResponse.
func (c *MultiRAGClient) CESearch(cmd *Command) (ResponseIf, error) {
	path, _ := cmd.Params["path"].(string)
	if path == "" {
		path = "datasets"
	}
	query, _ := cmd.Params["query"].(string)

	opts := &ce.SearchOptions{Query: query}
	if limit, ok := cmd.Params["limit"].(int); ok {
		opts.Limit = limit
	}

	result, err := c.ContextEngine.Search(context.Background(), path, opts)
	if err != nil {
		return nil, err
	}

	return &CESearchResponse{
		Code:         0,
		Total:        result.Total,
		Data:         ce.FormatNodes(result.Nodes, string(c.OutputFormat)),
		outputFormat: c.OutputFormat,
	}, nil
}
