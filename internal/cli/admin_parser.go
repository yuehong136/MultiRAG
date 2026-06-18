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

// ==================== Admin LIST ====================

func (p *Parser) parseAdminListCommand() (*Command, error) {
	p.nextToken() // consume LIST

	switch p.curToken.Type {
	case TokenServices:
		p.nextToken()
		if err := p.expectSemicolon(); err != nil {
			return nil, err
		}
		return NewCommand("list_services"), nil
	case TokenUsers:
		p.nextToken()
		if err := p.expectSemicolon(); err != nil {
			return nil, err
		}
		return NewCommand("list_users"), nil
	case TokenRoles:
		p.nextToken()
		if err := p.expectSemicolon(); err != nil {
			return nil, err
		}
		return NewCommand("list_roles"), nil
	case TokenVars:
		p.nextToken()
		if err := p.expectSemicolon(); err != nil {
			return nil, err
		}
		return NewCommand("list_variables"), nil
	case TokenConfigs:
		p.nextToken()
		if err := p.expectSemicolon(); err != nil {
			return nil, err
		}
		return NewCommand("list_configs"), nil
	case TokenEnvs:
		p.nextToken()
		if err := p.expectSemicolon(); err != nil {
			return nil, err
		}
		return NewCommand("list_environments"), nil
	case TokenDatasets:
		return p.parseListDatasets()
	case TokenAgents:
		return p.parseListAgents()
	case TokenTokens:
		return p.parseListTokens()
	default:
		return nil, fmt.Errorf("unknown LIST target: %s", p.curToken.Value)
	}
}

// ==================== Admin SHOW ====================

func (p *Parser) parseAdminShowCommand() (*Command, error) {
	p.nextToken() // consume SHOW

	switch p.curToken.Type {
	case TokenVersion:
		p.nextToken()
		if err := p.expectSemicolon(); err != nil {
			return nil, err
		}
		return NewCommand("show_version"), nil
	case TokenCurrent:
		p.nextToken()
		if p.curToken.Type != TokenUser {
			return nil, fmt.Errorf("expected USER after CURRENT")
		}
		p.nextToken()
		if err := p.expectSemicolon(); err != nil {
			return nil, err
		}
		return NewCommand("show_current_user"), nil
	case TokenUser:
		return p.parseShowUser()
	case TokenRole:
		return p.parseShowRole()
	case TokenVar:
		return p.parseShowVariable()
	case TokenService:
		return p.parseShowService()
	default:
		return nil, fmt.Errorf("unknown SHOW target: %s", p.curToken.Value)
	}
}

func (p *Parser) parseShowUser() (*Command, error) {
	p.nextToken() // consume USER

	// Check for PERMISSION
	if p.curToken.Type == TokenPermission {
		p.nextToken()
		userName, err := p.parseQuotedString()
		if err != nil {
			return nil, err
		}
		cmd := NewCommand("show_user_permission")
		cmd.Params["user_name"] = userName
		p.nextToken()
		if err := p.expectSemicolon(); err != nil {
			return nil, err
		}
		return cmd, nil
	}

	userName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("show_user")
	cmd.Params["user_name"] = userName

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseShowRole() (*Command, error) {
	p.nextToken() // consume ROLE
	roleName, err := p.parseIdentifier()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("show_role")
	cmd.Params["role_name"] = roleName

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseShowVariable() (*Command, error) {
	p.nextToken() // consume VAR
	varName, err := p.parseIdentifier()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("show_variable")
	cmd.Params["var_name"] = varName

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseShowService() (*Command, error) {
	p.nextToken() // consume SERVICE
	serviceNum, err := p.parseNumber()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("show_service")
	cmd.Params["number"] = serviceNum

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// ==================== Admin CREATE ====================

func (p *Parser) parseAdminCreateCommand() (*Command, error) {
	p.nextToken() // consume CREATE

	switch p.curToken.Type {
	case TokenUser:
		return p.parseCreateUser()
	case TokenRole:
		return p.parseCreateRole()
	case TokenModel:
		return p.parseCreateModelProvider()
	default:
		return nil, fmt.Errorf("unknown CREATE target: %s", p.curToken.Value)
	}
}

func (p *Parser) parseCreateUser() (*Command, error) {
	p.nextToken() // consume USER
	userName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	p.nextToken()
	password, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("create_user")
	cmd.Params["user_name"] = userName
	cmd.Params["password"] = password
	cmd.Params["role"] = "user"

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseCreateRole() (*Command, error) {
	p.nextToken() // consume ROLE
	roleName, err := p.parseIdentifier()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("create_role")
	cmd.Params["role_name"] = roleName

	p.nextToken()
	if p.curToken.Type == TokenDescription {
		p.nextToken()
		description, err := p.parseQuotedString()
		if err != nil {
			return nil, err
		}
		cmd.Params["description"] = description
		p.nextToken()
	}

	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseCreateModelProvider() (*Command, error) {
	p.nextToken() // consume MODEL
	if p.curToken.Type != TokenProvider {
		return nil, fmt.Errorf("expected PROVIDER")
	}
	p.nextToken()

	providerName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	p.nextToken()
	providerKey, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("create_model_provider")
	cmd.Params["provider_name"] = providerName
	cmd.Params["provider_key"] = providerKey

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// ==================== Admin DROP ====================

func (p *Parser) parseAdminDropCommand() (*Command, error) {
	p.nextToken() // consume DROP

	switch p.curToken.Type {
	case TokenUser:
		return p.parseDropUser()
	case TokenRole:
		return p.parseDropRole()
	case TokenModel:
		return p.parseDropModelProvider()
	case TokenToken:
		return p.parseDropToken()
	default:
		return nil, fmt.Errorf("unknown DROP target: %s", p.curToken.Value)
	}
}

func (p *Parser) parseDropUser() (*Command, error) {
	p.nextToken() // consume USER
	userName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("drop_user")
	cmd.Params["user_name"] = userName

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseDropRole() (*Command, error) {
	p.nextToken() // consume ROLE
	roleName, err := p.parseIdentifier()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("drop_role")
	cmd.Params["role_name"] = roleName

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseDropModelProvider() (*Command, error) {
	p.nextToken() // consume MODEL
	if p.curToken.Type != TokenProvider {
		return nil, fmt.Errorf("expected PROVIDER")
	}
	p.nextToken()

	providerName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("drop_model_provider")
	cmd.Params["provider_name"] = providerName

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// ==================== Admin ALTER ====================

func (p *Parser) parseAdminAlterCommand() (*Command, error) {
	p.nextToken() // consume ALTER

	switch p.curToken.Type {
	case TokenUser:
		return p.parseAlterUser()
	case TokenRole:
		return p.parseAlterRole()
	default:
		return nil, fmt.Errorf("unknown ALTER target: %s", p.curToken.Value)
	}
}

func (p *Parser) parseAlterUser() (*Command, error) {
	p.nextToken() // consume USER

	if p.curToken.Type == TokenActive {
		return p.parseActivateUser()
	}

	if p.curToken.Type == TokenPassword {
		p.nextToken()
		userName, err := p.parseQuotedString()
		if err != nil {
			return nil, err
		}

		p.nextToken()
		password, err := p.parseQuotedString()
		if err != nil {
			return nil, err
		}

		cmd := NewCommand("alter_user")
		cmd.Params["user_name"] = userName
		cmd.Params["password"] = password

		p.nextToken()
		if err := p.expectSemicolon(); err != nil {
			return nil, err
		}
		return cmd, nil
	}

	userName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	p.nextToken()
	if p.curToken.Type != TokenSet {
		return nil, fmt.Errorf("expected SET")
	}
	p.nextToken()
	if p.curToken.Type != TokenRole {
		return nil, fmt.Errorf("expected ROLE")
	}
	p.nextToken()

	roleName, err := p.parseIdentifier()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("alter_user_role")
	cmd.Params["user_name"] = userName
	cmd.Params["role_name"] = roleName

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseActivateUser() (*Command, error) {
	p.nextToken() // consume ACTIVE
	userName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	p.nextToken()
	// Accept 'on' or 'off' as identifier
	status := p.curToken.Value
	if status != "on" && status != "off" {
		return nil, fmt.Errorf("expected 'on' or 'off', got %s", p.curToken.Value)
	}

	cmd := NewCommand("activate_user")
	cmd.Params["user_name"] = userName
	cmd.Params["activate_status"] = status

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseAlterRole() (*Command, error) {
	p.nextToken() // consume ROLE
	roleName, err := p.parseIdentifier()
	if err != nil {
		return nil, err
	}

	p.nextToken()
	if p.curToken.Type != TokenSet {
		return nil, fmt.Errorf("expected SET")
	}
	p.nextToken()
	if p.curToken.Type != TokenDescription {
		return nil, fmt.Errorf("expected DESCRIPTION")
	}
	p.nextToken()

	description, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("alter_role")
	cmd.Params["role_name"] = roleName
	cmd.Params["description"] = description

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// ==================== Admin GRANT / REVOKE ====================

func (p *Parser) parseGrantCommand() (*Command, error) {
	p.nextToken() // consume GRANT

	if p.curToken.Type == TokenAdmin {
		return p.parseGrantAdmin()
	}

	return p.parseGrantPermission()
}

func (p *Parser) parseGrantAdmin() (*Command, error) {
	p.nextToken() // consume ADMIN
	userName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("grant_admin")
	cmd.Params["user_name"] = userName

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseGrantPermission() (*Command, error) {
	actions, err := p.parseIdentifierList()
	if err != nil {
		return nil, err
	}

	if p.curToken.Type != TokenOn {
		return nil, fmt.Errorf("expected ON")
	}
	p.nextToken()

	resource, err := p.parseIdentifier()
	if err != nil {
		return nil, err
	}

	p.nextToken()
	if p.curToken.Type != TokenTo {
		return nil, fmt.Errorf("expected TO")
	}
	p.nextToken()
	if p.curToken.Type != TokenRole {
		return nil, fmt.Errorf("expected ROLE")
	}
	p.nextToken()

	roleName, err := p.parseIdentifier()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("grant_permission")
	cmd.Params["actions"] = actions
	cmd.Params["resource"] = resource
	cmd.Params["role_name"] = roleName

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseRevokeCommand() (*Command, error) {
	p.nextToken() // consume REVOKE

	if p.curToken.Type == TokenAdmin {
		return p.parseRevokeAdmin()
	}

	return p.parseRevokePermission()
}

func (p *Parser) parseRevokeAdmin() (*Command, error) {
	p.nextToken() // consume ADMIN
	userName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("revoke_admin")
	cmd.Params["user_name"] = userName

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseRevokePermission() (*Command, error) {
	actions, err := p.parseIdentifierList()
	if err != nil {
		return nil, err
	}

	if p.curToken.Type != TokenOn {
		return nil, fmt.Errorf("expected ON")
	}
	p.nextToken()

	resource, err := p.parseIdentifier()
	if err != nil {
		return nil, err
	}

	p.nextToken()
	if p.curToken.Type != TokenFrom {
		return nil, fmt.Errorf("expected FROM")
	}
	p.nextToken()
	if p.curToken.Type != TokenRole {
		return nil, fmt.Errorf("expected ROLE")
	}
	p.nextToken()

	roleName, err := p.parseIdentifier()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("revoke_permission")
	cmd.Params["actions"] = actions
	cmd.Params["resource"] = resource
	cmd.Params["role_name"] = roleName

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// ==================== Admin SET ====================

func (p *Parser) parseAdminSetCommand() (*Command, error) {
	p.nextToken() // consume SET

	if p.curToken.Type == TokenVar {
		return p.parseSetVariable()
	}

	return nil, fmt.Errorf("unknown SET target: %s", p.curToken.Value)
}

func (p *Parser) parseSetVariable() (*Command, error) {
	p.nextToken() // consume VAR
	varName, err := p.parseIdentifier()
	if err != nil {
		return nil, err
	}

	p.nextToken()
	varValue, err := p.parseIdentifier()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("set_variable")
	cmd.Params["var_name"] = varName
	cmd.Params["var_value"] = varValue

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// ==================== Admin GENERATE ====================

// parseGenerateCommand parses: GENERATE TOKEN FOR USER '<email>';  (admin mode)
func (p *Parser) parseGenerateCommand() (*Command, error) {
	p.nextToken() // consume GENERATE
	if p.curToken.Type != TokenToken {
		return nil, fmt.Errorf("expected TOKEN")
	}
	p.nextToken()
	if p.curToken.Type != TokenFor {
		return nil, fmt.Errorf("expected FOR")
	}
	p.nextToken()
	if p.curToken.Type != TokenUser {
		return nil, fmt.Errorf("expected USER")
	}
	p.nextToken()

	userName, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("generate_token")
	cmd.Params["user_name"] = userName

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// ==================== Admin service control ====================

func (p *Parser) parseStartupCommand() (*Command, error) {
	p.nextToken() // consume STARTUP
	if p.curToken.Type != TokenService {
		return nil, fmt.Errorf("expected SERVICE")
	}
	p.nextToken()

	serviceNum, err := p.parseNumber()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("startup_service")
	cmd.Params["number"] = serviceNum

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseShutdownCommand() (*Command, error) {
	p.nextToken() // consume SHUTDOWN
	if p.curToken.Type != TokenService {
		return nil, fmt.Errorf("expected SERVICE")
	}
	p.nextToken()

	serviceNum, err := p.parseNumber()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("shutdown_service")
	cmd.Params["number"] = serviceNum

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseRestartCommand() (*Command, error) {
	p.nextToken() // consume RESTART
	if p.curToken.Type != TokenService {
		return nil, fmt.Errorf("expected SERVICE")
	}
	p.nextToken()

	serviceNum, err := p.parseNumber()
	if err != nil {
		return nil, err
	}

	cmd := NewCommand("restart_service")
	cmd.Params["number"] = serviceNum

	p.nextToken()
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}
