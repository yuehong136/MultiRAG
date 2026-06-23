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

import "fmt"

// PasswordPromptFunc is a function type for password input
type PasswordPromptFunc func(prompt string) (string, error)

// MultiRAGClient handles API interactions with the MultiRAG server
type MultiRAGClient struct {
	HTTPClient     *HTTPClient
	ServerType     string             // "admin" or "user"
	PasswordPrompt PasswordPromptFunc // Function for password input
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

	return &MultiRAGClient{
		HTTPClient: httpClient,
		ServerType: serverType,
	}
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
	// TODO: Implement other user commands
	default:
		return nil, fmt.Errorf("command '%s' would be executed with API", cmd.Type)
	}
}
