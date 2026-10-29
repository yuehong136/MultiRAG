package cli

import (
	"fmt"
	"strings"
)

// parseServerCommand handles local registry commands before either SQL grammar.
// It does not accept partial commands or silently discard trailing statements.
func (p *Parser) parseServerCommand() (*Command, bool, error) {
	verb := p.curToken.Type
	next := p.peekToken.Type
	if verb == TokenLogin && next == TokenAdmin {
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
	if verb == TokenRetrieve {
		cmd, err := p.parseSearchCommand()
		return cmd, true, err
	}
	if verb == TokenShow && next == TokenCurrent {
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
	if verb == TokenSave && next == TokenConfig {
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
	if (verb != TokenAdd && verb != TokenDelete && verb != TokenShow && verb != TokenList) || (next != TokenAPI && next != TokenAdmin) {
		return nil, false, nil
	}
	kind := "api"
	if next == TokenAdmin {
		kind = "admin"
	}
	p.nextToken()
	p.nextToken()
	if p.curToken.Type != TokenServer {
		return nil, true, fmt.Errorf("expected SERVER")
	}
	p.nextToken()
	if verb == TokenList && kind != "api" {
		return nil, true, fmt.Errorf("LIST requires API SERVER")
	}
	action := map[int]string{TokenAdd: "add", TokenDelete: "delete", TokenShow: "show", TokenList: "list"}[verb]
	cmd := NewCommand(action + "_" + kind + "_server")
	if kind == "api" && verb != TokenList {
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
	if verb == TokenAdd {
		if p.curToken.Type != TokenHost {
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
