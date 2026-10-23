package cli

import "fmt"

func (p *Parser) parseMediaCommand(kind string) (*Command, error) {
	p.nextToken()
	if p.curToken.Type != TokenWith {
		return nil, fmt.Errorf("expected WITH after %s", kind)
	}
	p.nextToken()
	name, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	p.nextToken()
	token, field := TokenAudio, "audio_file"
	if kind == "tts" {
		token, field = TokenText, "text"
	}
	if kind == "ocr" {
		token, field = TokenFile, "file"
	}
	if p.curToken.Type != token {
		return nil, fmt.Errorf("expected AUDIO, TEXT or FILE input for %s", kind)
	}
	p.nextToken()
	value, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	p.nextToken()
	// Check the current token: expectSemicolon alone also accepts a semicolon after an extra token.
	if p.curToken.Type != TokenSemicolon {
		return nil, fmt.Errorf("expected semicolon")
	}
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	cmd := NewCommand(kind + "_user_command")
	cmd.Params["composite_model_name"] = name
	cmd.Params[field] = value
	return cmd, nil
}
