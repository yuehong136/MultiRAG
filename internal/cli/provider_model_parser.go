package cli

import (
	"fmt"
	"slices"
)

// parseAddModel parses a custom model declaration using the established capability names.
func (p *Parser) parseAddModel() (*Command, error) {
	p.nextToken()
	name, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	p.nextToken()
	if p.curToken.Type != TokenTo {
		return nil, fmt.Errorf("expected TO")
	}
	p.nextToken()
	if p.curToken.Type != TokenProvider {
		return nil, fmt.Errorf("expected PROVIDER")
	}
	p.nextToken()
	provider, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	p.nextToken()
	if p.curToken.Type != TokenInstance {
		return nil, fmt.Errorf("expected INSTANCE")
	}
	p.nextToken()
	instance, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	p.nextToken()
	if p.curToken.Type != TokenWith {
		return nil, fmt.Errorf("expected WITH")
	}
	p.nextToken()
	cmd := NewCommand("add_custom_model")
	cmd.Params["model_name"], cmd.Params["provider_name"], cmd.Params["instance_name"] = name, provider, instance
	var types []string
	tokens := 0
	thinking := false
	for p.curToken.Type != TokenSemicolon && p.curToken.Type != TokenEOF {
		selected := ""
		switch p.curToken.Type {
		case TokenChat:
			selected = "chat"
		case TokenVLM:
			selected = "image2text"
		case TokenEmbedding:
			selected = "embedding"
		case TokenReranker:
			selected = "rerank"
		case TokenASR:
			selected = "speech2text"
		case TokenTTS:
			selected = "tts"
		case TokenOCR:
			selected = "ocr"
		case TokenTokens:
			if tokens != 0 {
				return nil, fmt.Errorf("duplicate token limit")
			}
			p.nextToken()
			value, err := p.parseNumber()
			if err != nil || value <= 0 {
				return nil, fmt.Errorf("positive integer token limit required")
			}
			tokens = value
		case TokenThink:
			if thinking {
				return nil, fmt.Errorf("duplicate THINK")
			}
			thinking = true
		default:
			return nil, fmt.Errorf("unknown model option: %s", p.curToken.Value)
		}
		if selected != "" {
			if slices.Contains(types, selected) {
				return nil, fmt.Errorf("duplicate model capability")
			}
			types = append(types, selected)
		}
		p.nextToken()
	}
	if len(types) == 0 || tokens == 0 {
		return nil, fmt.Errorf("model type and token limit required")
	}
	if thinking && !slices.Contains(types, "chat") && !slices.Contains(types, "image2text") {
		return nil, fmt.Errorf("THINK requires chat or vision")
	}
	if p.curToken.Type != TokenSemicolon {
		return nil, fmt.Errorf("expected semicolon after options")
	}
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	p.nextToken()
	if p.curToken.Type != TokenEOF {
		return nil, fmt.Errorf("unexpected trailing model option")
	}
	cmd.Params["model_type"], cmd.Params["max_tokens"] = types[0], tokens
	cmd.Params["model_types"] = types
	if thinking {
		cmd.Params["support_think"] = true
	}
	return cmd, nil
}

// DROP MODEL 'name' FROM 'provider' 'instance'; also accepts explicit keywords.
func (p *Parser) parseDropInstanceModel() (*Command, error) {
	p.nextToken()
	model, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	p.nextToken()
	if p.curToken.Type != TokenFrom {
		return nil, fmt.Errorf("expected FROM")
	}
	p.nextToken()
	if p.curToken.Type == TokenProvider {
		p.nextToken()
	}
	provider, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	p.nextToken()
	if p.curToken.Type == TokenInstance {
		p.nextToken()
	}
	instance, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	p.nextToken()
	if p.curToken.Type == TokenSemicolon {
		p.nextToken()
	}
	if p.curToken.Type != TokenEOF {
		return nil, fmt.Errorf("unexpected trailing DROP MODEL input")
	}
	cmd := NewCommand("drop_instance_model")
	cmd.Params["model_name"], cmd.Params["provider_name"], cmd.Params["instance_name"] = model, provider, instance
	return cmd, nil
}
