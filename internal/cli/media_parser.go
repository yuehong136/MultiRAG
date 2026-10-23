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
	if kind == "ocr" && p.curToken.Type == TokenURL {
		token, field = TokenURL, "url"
	}
	if p.curToken.Type != token {
		return nil, fmt.Errorf("expected AUDIO, TEXT, FILE or OCR URL input for %s", kind)
	}
	p.nextToken()
	value, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	p.nextToken()

	cmd := NewCommand(kind + "_user_command")
	cmd.Params["composite_model_name"] = name
	cmd.Params[field] = value
	seen := map[int]bool{}
	for p.curToken.Type != TokenSemicolon && p.curToken.Type != TokenEOF {
		option := p.curToken.Type
		if seen[option] {
			return nil, fmt.Errorf("duplicate media option: %s", p.curToken.Value)
		}
		seen[option] = true
		key := ""
		switch option {
		case TokenParam:
			if kind == "ocr" {
				return nil, fmt.Errorf("PARAM is available for ASR and TTS")
			}
			key = "param_str"
		case TokenFormat, TokenSave, TokenPlay:
			if kind != "tts" {
				return nil, fmt.Errorf("FORMAT, SAVE and PLAY are available for TTS")
			}
			if option == TokenPlay {
				cmd.Params["play"] = true
				p.nextToken()
				continue
			}
			if option == TokenFormat {
				key = "format"
			} else {
				key = "save_path"
			}
		default:
			return nil, fmt.Errorf("unexpected media option: %s", p.curToken.Value)
		}
		p.nextToken()
		if p.curToken.Type != TokenQuotedString && (option == TokenParam || p.curToken.Type != TokenIdentifier) {
			return nil, fmt.Errorf("expected value for media option")
		}
		cmd.Params[key] = p.curToken.Value
		if option == TokenSave {
			cmd.Params["save"] = true
		}
		p.nextToken()
	}
	if p.curToken.Type != TokenSemicolon {
		return nil, fmt.Errorf("expected semicolon")
	}
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}
