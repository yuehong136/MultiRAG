package cli

import (
	"fmt"
	"strings"
)

// parseServerCommand handles local registry commands before either SQL grammar.
// It does not accept partial commands or silently discard trailing statements.
func (p *Parser) parseServerCommand() (*Command, bool, error) {
	verb := strings.ToUpper(p.curToken.Value)
	next := strings.ToUpper(p.peekToken.Value)
	if verb == "LOGIN" && next == "ADMIN" {
		p.nextToken()
		p.nextToken()
		email, err := p.parseQuotedString()
		if err != nil {
			return nil, true, err
		}
		p.nextToken()
		cmd := NewCommand("login_admin")
		cmd.Params["email"] = email
		if p.curToken.Type == TokenWith {
			p.nextToken()
			if p.curToken.Type != TokenPassword {
				return nil, true, fmt.Errorf("expected PASSWORD")
			}
			p.nextToken()
			password, err := p.parseQuotedString()
			if err != nil {
				return nil, true, err
			}
			cmd.Params["password"] = password
			p.nextToken()
		}
		return cmd, true, p.finishServerCommand()
	}
	if verb == "RETRIEVE" {
		cmd, err := p.parseSearchCommand()
		return cmd, true, err
	}
	if verb == "SHOW" && next == "CURRENT" {
		// Existing explicit MODEL/USER forms remain in their owning grammars.
		// Only consume the aggregate when CURRENT is followed by end/semicolon.
		saved := *p
		lexer := *p.lexer
		p.nextToken()
		p.nextToken()
		if p.curToken.Type == TokenEOF || p.curToken.Type == TokenSemicolon {
			return NewCommand("show_current"), true, p.finishServerCommand()
		}
		*p = saved
		*p.lexer = lexer
		return nil, false, nil
	}
	if verb == "SAVE" && next == "CONFIG" {
		p.nextToken()
		p.nextToken()
		if p.curToken.Type != TokenAs {
			return nil, true, fmt.Errorf("expected AS after CONFIG")
		}
		p.nextToken()
		path, err := p.parseQuotedString()
		if err != nil {
			return nil, true, err
		}
		p.nextToken()
		cmd := NewCommand("save_config")
		cmd.Params["path"] = path
		return cmd, true, p.finishServerCommand()
	}
	if (verb != "ADD" && verb != "DELETE" && verb != "SHOW" && verb != "LIST") || (next != "API" && next != "ADMIN") {
		return nil, false, nil
	}
	kind := strings.ToLower(next)
	p.nextToken()
	p.nextToken()
	if !strings.EqualFold(p.curToken.Value, "SERVER") {
		return nil, true, fmt.Errorf("expected SERVER")
	}
	p.nextToken()
	if verb == "LIST" && kind != "api" {
		return nil, true, fmt.Errorf("LIST requires API SERVER")
	}
	cmd := NewCommand(strings.ToLower(verb) + "_" + kind + "_server")
	if kind == "api" && verb != "LIST" {
		name, err := p.parseQuotedString()
		if err != nil {
			return nil, true, err
		}
		p.nextToken()
		if strings.TrimSpace(name) == "" {
			return nil, true, fmt.Errorf("server name required")
		}
		cmd.Params["name"] = name
	}
	if verb == "ADD" {
		if !strings.EqualFold(p.curToken.Value, "HOST") {
			return nil, true, fmt.Errorf("expected HOST")
		}
		p.nextToken()
		host, err := p.parseQuotedString()
		if err != nil {
			return nil, true, err
		}
		p.nextToken()
		if _, _, err = validatedServerAddress(host); err != nil {
			return nil, true, err
		}
		cmd.Params["host"] = host
		if kind == "api" && p.curToken.Type == TokenToken {
			p.nextToken()
			token, err := p.parseQuotedString()
			if err != nil {
				return nil, true, err
			}
			cmd.Params["token"] = token
			p.nextToken()
		}
	}
	return cmd, true, p.finishServerCommand()
}

func (p *Parser) finishServerCommand() error {
	if p.curToken.Type != TokenSemicolon {
		return fmt.Errorf("expected semicolon")
	}
	p.nextToken()
	if p.curToken.Type != TokenEOF {
		return fmt.Errorf("unexpected input after command")
	}
	return nil
}
