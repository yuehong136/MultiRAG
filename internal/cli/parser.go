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
	"fmt"
	"strconv"
	"strings"
)

// Parser implements a recursive descent parser for MultiRAG CLI commands.
// Command parsing is split by mode: admin-mode parse functions live in
// admin_parser.go, user-mode ones in user_parser.go, and helpers / shared
// leaf parsers stay here.
type Parser struct {
	lexer     *Lexer
	curToken  Token
	peekToken Token
}

// NewParser creates a new parser
func NewParser(input string) *Parser {
	l := NewLexer(input)
	p := &Parser{lexer: l}
	// Read two tokens to initialize curToken and peekToken
	p.nextToken()
	p.nextToken()
	return p
}

func (p *Parser) nextToken() {
	p.curToken = p.peekToken
	p.peekToken = p.lexer.NextToken()
}

// Parse parses the input and returns a Command. adminCommand selects the
// admin grammar (true) or the user grammar (false).
func (p *Parser) Parse(adminCommand bool) (*Command, error) {
	if p.curToken.Type == TokenEOF {
		return nil, nil
	}

	// Check for meta commands (backslash commands)
	if p.curToken.Type == TokenIdentifier && strings.HasPrefix(p.curToken.Value, "\\") {
		return p.parseMetaCommand()
	}

	// Check for Context Engine commands (ls/search) entered through the SQL
	// parser. The primary entry point is cli.go's executeContextEngine (which
	// also supports flags and `cat`); this SQL-parser path is kept aligned with
	// ragflow and is reached only when Parse() is invoked directly.
	if p.curToken.Type == TokenIdentifier && isCECommand(p.curToken.Value) {
		return p.parseCECommand()
	}

	// Parse SQL-like command
	return p.parseSQLCommand(adminCommand)
}

func (p *Parser) parseMetaCommand() (*Command, error) {
	cmd := NewCommand("meta")
	cmdName := strings.TrimPrefix(p.curToken.Value, "\\")
	cmd.Params["command"] = strings.ToLower(cmdName)

	// Parse arguments
	var args []string
	p.nextToken()
	for p.curToken.Type != TokenEOF {
		args = append(args, p.curToken.Value)
		p.nextToken()
	}
	cmd.Params["args"] = args

	return cmd, nil
}

func (p *Parser) parseSQLCommand(adminCommand bool) (*Command, error) {
	if p.curToken.Type != TokenIdentifier && !isKeyword(p.curToken.Type) {
		return nil, fmt.Errorf("expected command, got %s", p.curToken.Value)
	}

	if adminCommand {
		return p.parseAdminCommand()
	}
	return p.parseUserCommand()
}

// parseAdminCommand dispatches admin-mode commands.
func (p *Parser) parseAdminCommand() (*Command, error) {
	switch p.curToken.Type {
	case TokenLogin:
		return p.parseLoginUser()
	case TokenLogout:
		return p.parseLogout()
	case TokenPing:
		return p.parsePingServer()
	case TokenList:
		return p.parseAdminListCommand()
	case TokenShow:
		return p.parseAdminShowCommand()
	case TokenCreate:
		return p.parseAdminCreateCommand()
	case TokenDrop:
		return p.parseAdminDropCommand()
	case TokenAlter:
		return p.parseAdminAlterCommand()
	case TokenGrant:
		return p.parseGrantCommand()
	case TokenRevoke:
		return p.parseRevokeCommand()
	case TokenSet:
		return p.parseAdminSetCommand()
	case TokenGenerate:
		return p.parseGenerateCommand()
	case TokenBenchmark:
		return p.parseBenchmarkCommand()
	case TokenStartup:
		return p.parseStartupCommand()
	case TokenShutdown:
		return p.parseShutdownCommand()
	case TokenRestart:
		return p.parseRestartCommand()
	default:
		return nil, fmt.Errorf("unknown command: %s", p.curToken.Value)
	}
}

// parseUserCommand dispatches user-mode commands.
func (p *Parser) parseUserCommand() (*Command, error) {
	switch p.curToken.Type {
	case TokenLogin:
		return p.parseLoginUser()
	case TokenLogout:
		return p.parseLogout()
	case TokenPing:
		return p.parsePingServer()
	case TokenRegister:
		return p.parseRegisterCommand()
	case TokenList:
		return p.parseListCommand()
	case TokenShow:
		return p.parseShowCommand()
	case TokenCreate:
		return p.parseCreateCommand()
	case TokenDrop:
		return p.parseDropCommand()
	case TokenAdd:
		return p.parseAddCommand()
	case TokenDelete:
		return p.parseDeleteCommand()
	case TokenAlter:
		return p.parseUserAlterCommand()
	case TokenSet:
		return p.parseSetCommand()
	case TokenReset:
		return p.parseResetCommand()
	case TokenImport:
		return p.parseImportCommand()
	case TokenInsert:
		return p.parseInsertCommand()
	case TokenSearch:
		return p.parseSearchCommand()
	case TokenParse:
		return p.parseParseCommand()
	case TokenBenchmark:
		return p.parseBenchmarkCommand()
	case TokenEnable:
		return p.parseEnableCommand()
	case TokenDisable:
		return p.parseDisableCommand()
	case TokenChat:
		return p.parseChatCommand()
	case TokenThink:
		return p.parseThinkCommand()
	case TokenUse:
		return p.parseUseCommand()
	case TokenUpdate:
		return p.parseUpdateCommand()
	case TokenRemove:
		return p.parseRemoveCommand()
	default:
		return nil, fmt.Errorf("unknown command: %s", p.curToken.Value)
	}
}

func (p *Parser) expectPeek(tokenType int) error {
	if p.peekToken.Type != tokenType {
		return fmt.Errorf("expected %s, got %s", tokenTypeToString(tokenType), p.peekToken.Value)
	}
	p.nextToken()
	return nil
}

func (p *Parser) expectSemicolon() error {
	if p.curToken.Type == TokenSemicolon {
		return nil
	}
	if p.peekToken.Type == TokenSemicolon {
		p.nextToken()
		return nil
	}
	return fmt.Errorf("expected semicolon")
}

func isKeyword(tokenType int) bool {
	return tokenType >= TokenLogin && tokenType <= TokenTag
}

// isCECommand reports whether the given word selects a Context Engine command.
func isCECommand(s string) bool {
	switch strings.ToUpper(s) {
	case "LS", "LIST", "SEARCH":
		return true
	}
	return false
}

// parseCECommand parses a Context Engine command (ls/search) into a ce_ls /
// ce_search Command executed by MultiRAGClient.CEList / CESearch.
func (p *Parser) parseCECommand() (*Command, error) {
	switch strings.ToUpper(p.curToken.Value) {
	case "LS", "LIST":
		return p.parseCEListCommand()
	case "SEARCH":
		return p.parseCESearchCommand()
	default:
		return nil, fmt.Errorf("unknown ContextEngine command: %s", p.curToken.Value)
	}
}

// parseCEListCommand parses: ls [path]   (defaults to "datasets").
func (p *Parser) parseCEListCommand() (*Command, error) {
	p.nextToken() // consume LS/LIST

	cmd := NewCommand("ce_ls")

	// Accept an identifier/quoted path, or the "datasets" keyword as a path.
	if p.curToken.Type == TokenIdentifier || p.curToken.Type == TokenQuotedString ||
		p.curToken.Type == TokenDatasets {
		path := p.curToken.Value
		if p.curToken.Type == TokenQuotedString {
			path = strings.Trim(path, "\"'")
		}
		cmd.Params["path"] = path
		p.nextToken()
	} else {
		cmd.Params["path"] = "datasets"
	}

	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}

	return cmd, nil
}

// parseCESearchCommand parses: search <query> [in <path>].
func (p *Parser) parseCESearchCommand() (*Command, error) {
	p.nextToken() // consume SEARCH

	cmd := NewCommand("ce_search")

	if p.curToken.Type != TokenIdentifier && p.curToken.Type != TokenQuotedString {
		return nil, fmt.Errorf("expected query after SEARCH")
	}

	query := p.curToken.Value
	if p.curToken.Type == TokenQuotedString {
		query = strings.Trim(query, "\"'")
	}
	cmd.Params["query"] = query
	p.nextToken()

	// Optional "in <path>" clause.
	if p.curToken.Type == TokenIdentifier && strings.ToUpper(p.curToken.Value) == "IN" {
		p.nextToken() // consume IN
		if p.curToken.Type != TokenIdentifier && p.curToken.Type != TokenQuotedString {
			return nil, fmt.Errorf("expected path after IN")
		}
		path := p.curToken.Value
		if p.curToken.Type == TokenQuotedString {
			path = strings.Trim(path, "\"'")
		}
		cmd.Params["path"] = path
		p.nextToken()
	} else {
		cmd.Params["path"] = "datasets"
	}

	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}

	return cmd, nil
}

// Helper functions for parsing
func (p *Parser) parseQuotedString() (string, error) {
	if p.curToken.Type != TokenQuotedString {
		return "", fmt.Errorf("expected quoted string, got %s", p.curToken.Value)
	}
	return p.curToken.Value, nil
}

func (p *Parser) parseIdentifier() (string, error) {
	if p.curToken.Type != TokenIdentifier {
		return "", fmt.Errorf("expected identifier, got %s", p.curToken.Value)
	}
	return p.curToken.Value, nil
}

func (p *Parser) parseNumber() (int, error) {
	if p.curToken.Type != TokenNumber {
		return 0, fmt.Errorf("expected number, got %s", p.curToken.Value)
	}
	return strconv.Atoi(p.curToken.Value)
}

func (p *Parser) parseIdentifierList() ([]string, error) {
	var list []string

	ident, err := p.parseIdentifier()
	if err != nil {
		return nil, err
	}
	list = append(list, ident)
	p.nextToken()

	for p.curToken.Type == TokenComma {
		p.nextToken()
		ident, err := p.parseIdentifier()
		if err != nil {
			return nil, err
		}
		list = append(list, ident)
		p.nextToken()
	}

	return list, nil
}

func tokenTypeToString(t int) string {
	// Simplified for error messages
	return fmt.Sprintf("token(%d)", t)
}

// ==================== Shared leaf parsers (both modes) ====================

func (p *Parser) parseLoginUser() (*Command, error) {
	cmd := NewCommand("login_user")

	p.nextToken() // consume LOGIN
	if p.curToken.Type != TokenUser {
		return nil, fmt.Errorf("expected USER after LOGIN")
	}

	p.nextToken()
	email, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	cmd.Params["email"] = email

	p.nextToken()
	// Optional: WITH PASSWORD 'password'
	if p.curToken.Type == TokenWith {
		p.nextToken()
		if p.curToken.Type != TokenPassword {
			return nil, fmt.Errorf("expected PASSWORD after WITH")
		}
		p.nextToken()
		password, err := p.parseQuotedString()
		if err != nil {
			return nil, err
		}
		cmd.Params["password"] = password
		p.nextToken()
	}

	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}

	return cmd, nil
}

// parseLogout parses LOGOUT[;] for both admin and user modes.
func (p *Parser) parseLogout() (*Command, error) {
	cmd := NewCommand("logout")
	p.nextToken() // consume LOGOUT
	// Semicolon is optional for LOGOUT
	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}
	return cmd, nil
}

// parseCommonListProviders parses (shared by admin and user LIST):
//
//	LIST AVAILABLE PROVIDERS;
func (p *Parser) parseCommonListProviders() (*Command, error) {
	p.nextToken() // consume AVAILABLE

	if p.curToken.Type != TokenProviders {
		return nil, fmt.Errorf("expected PROVIDERS")
	}

	return NewCommand("list_available_providers"), nil
}

// parseListModelsOfProvider parses (shared by admin and user LIST):
//
//	LIST MODELS FROM '<provider>';
//	LIST MODELS FROM '<provider>' '<instance>';
func (p *Parser) parseListModelsOfProvider() (*Command, error) {
	if p.curToken.Type != TokenModels {
		return nil, fmt.Errorf("expected MODELS")
	}

	p.nextToken()
	if p.curToken.Type != TokenFrom {
		return nil, fmt.Errorf("expected FROM")
	}
	p.nextToken()

	// Parse first quoted string (could be instance_name or provider_name)
	firstName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	p.nextToken()

	// Check if there's a second quoted string (instance_name)
	// If so, format is: LIST MODELS FROM <provider_name> <instance_name>
	// If not, format is: LIST MODELS FROM <provider_name>
	if p.curToken.Type == TokenQuotedString {
		// Two arguments: provider_name and instance_name
		instanceName, err := p.parseQuotedString()
		if err != nil {
			return nil, err
		}
		cmd := NewCommand("list_instance_models")
		cmd.Params["instance_name"] = instanceName
		cmd.Params["provider_name"] = firstName
		p.nextToken()
		if p.curToken.Type == TokenSemicolon {
			p.nextToken()
		}
		return cmd, nil
	}

	// Only one argument: provider_name
	cmd := NewCommand("list_provider_models")
	cmd.Params["provider_name"] = firstName
	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}
	return cmd, nil
}

// parseShowProvider parses (shared by admin and user SHOW):
//
//	SHOW PROVIDER '<provider>';
func (p *Parser) parseShowProvider() (*Command, error) {
	p.nextToken() // consume PROVIDER

	providerName, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected provider name: %w", err)
	}

	cmd := NewCommand("show_provider")
	cmd.Params["provider_name"] = providerName

	p.nextToken()
	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}
	return cmd, nil
}

// parseShowModel parses (shared by admin and user SHOW):
//
//	SHOW MODEL '<model>' FROM '<provider>';
func (p *Parser) parseShowModel() (*Command, error) {
	p.nextToken() // consume MODEL

	modelName, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected model name: %w", err)
	}

	cmd := NewCommand("show_model")
	cmd.Params["model_name"] = modelName

	p.nextToken() // consume model_name

	if p.curToken.Type != TokenFrom {
		return nil, fmt.Errorf("expected FROM")
	}
	p.nextToken() // consume FROM
	providerName, err := p.parseQuotedString()
	if err != nil {
		return nil, fmt.Errorf("expected provider name: %w", err)
	}
	cmd.Params["provider_name"] = providerName
	p.nextToken() // consume provider name
	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}
	return cmd, nil
}

func (p *Parser) parsePingServer() (*Command, error) {
	cmd := NewCommand("ping_server")
	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// parseListDatasets parses:
//
//	LIST DATASETS;                 (user mode, self-service -> list_user_datasets)
//	LIST DATASETS OF '<email>';    (admin mode, on behalf of a user -> list_datasets)
func (p *Parser) parseListDatasets() (*Command, error) {
	cmd := NewCommand("list_user_datasets")
	p.nextToken() // consume DATASETS

	if p.curToken.Type == TokenSemicolon {
		return cmd, nil
	}

	if p.curToken.Type == TokenOf {
		p.nextToken()
		userName, err := p.parseQuotedString()
		if err != nil {
			return nil, err
		}
		cmd = NewCommand("list_datasets")
		cmd.Params["user_name"] = userName
		p.nextToken()
	}

	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// parseListAgents parses LIST AGENTS [OF '<email>'];
func (p *Parser) parseListAgents() (*Command, error) {
	p.nextToken() // consume AGENTS

	if p.curToken.Type == TokenSemicolon {
		return NewCommand("list_user_agents"), nil
	}

	if p.curToken.Type != TokenOf {
		return nil, fmt.Errorf("expected OF")
	}
	p.nextToken()

	userName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("list_agents")
	cmd.Params["user_name"] = userName

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// parseListTokens parses:
//
//	LIST TOKENS;                 (user mode, self-service)
//	LIST TOKENS OF '<email>';    (admin mode, on behalf of a user)
func (p *Parser) parseListTokens() (*Command, error) {
	p.nextToken() // consume TOKENS
	cmd := NewCommand("list_tokens")

	if p.curToken.Type == TokenOf {
		p.nextToken()
		userName, err := p.parseQuotedString()
		if err != nil {
			return nil, err
		}
		cmd.Params["user_name"] = userName
		p.nextToken()
	}

	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// parseDropToken parses:
//
//	DROP TOKEN '<token>';                 (user mode, self-service)
//	DROP TOKEN '<token>' OF '<email>';    (admin mode, on behalf of a user)
func (p *Parser) parseDropToken() (*Command, error) {
	p.nextToken() // consume TOKEN
	token, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("drop_token")
	cmd.Params["token"] = token

	p.nextToken()
	if p.curToken.Type == TokenOf {
		p.nextToken()
		userName, err := p.parseQuotedString()
		if err != nil {
			return nil, err
		}
		cmd.Params["user_name"] = userName
		p.nextToken()
	}

	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseBenchmarkCommand() (*Command, error) {
	cmd := NewCommand("benchmark")

	p.nextToken() // consume BENCHMARK
	concurrency, err := p.parseNumber()
	if err != nil {
		return nil, err
	}
	cmd.Params["concurrency"] = concurrency

	p.nextToken()
	iterations, err := p.parseNumber()
	if err != nil {
		return nil, err
	}
	cmd.Params["iterations"] = iterations

	p.nextToken()
	// Parse user_statement
	nestedCmd, err := p.parseUserStatement()
	if err != nil {
		return nil, err
	}
	cmd.Params["command"] = nestedCmd

	return cmd, nil
}

func (p *Parser) parseUserStatement() (*Command, error) {
	switch p.curToken.Type {
	case TokenPing:
		return p.parsePingServer()
	case TokenShow:
		return p.parseShowCommand()
	case TokenCreate:
		return p.parseCreateCommand()
	case TokenDrop:
		return p.parseDropCommand()
	case TokenAlter:
		return p.parseUserAlterCommand()
	case TokenSet:
		return p.parseSetCommand()
	case TokenReset:
		return p.parseResetCommand()
	case TokenList:
		return p.parseListCommand()
	case TokenParse:
		return p.parseParseCommand()
	case TokenImport:
		return p.parseImportCommand()
	case TokenInsert:
		return p.parseInsertCommand()
	case TokenSearch:
		return p.parseSearchCommand()
	case TokenUpdate:
		return p.parseUpdateCommand()
	case TokenRemove:
		return p.parseRemoveCommand()
	default:
		return nil, fmt.Errorf("invalid user statement: %s", p.curToken.Value)
	}
}
