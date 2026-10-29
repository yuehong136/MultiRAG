package cli

import (
	"fmt"
	"slices"
	"strings"
)

type customModelDeclaration struct {
	Name      string   `json:"model_name"`
	Types     []string `json:"model_types"`
	MaxTokens int      `json:"max_tokens"`
	Thinking  *bool    `json:"thinking,omitempty"`
}

func parseModelNames(raw string) ([]string, error) {
	names := strings.Fields(raw)
	if len(names) == 0 || len(names) > 100 {
		return nil, fmt.Errorf("between 1 and 100 model names required")
	}
	seen := make(map[string]bool, len(names))
	for _, name := range names {
		if seen[name] {
			return nil, fmt.Errorf("duplicate model name: %s", name)
		}
		seen[name] = true
	}
	return names, nil
}

// parseAddModel parses a custom model declaration using the established capability names.
func (p *Parser) parseAddModel() (*Command, error) {
	p.nextToken()
	name, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	names, err := parseModelNames(name)
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
	cmd.Params["provider_name"], cmd.Params["instance_name"] = provider, instance
	models := make([]customModelDeclaration, 0, len(names))
	for _, modelName := range names {
		var types []string
		tokens := 0
		tokenLimitSet := false
		thinking := false
		for p.curToken.Type != TokenComma && p.curToken.Type != TokenSemicolon && p.curToken.Type != TokenEOF {
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
			case TokenDocParse:
				selected = "doc_parse"
			case TokenToken, TokenTokens:
				if tokenLimitSet {
					return nil, fmt.Errorf("duplicate token limit")
				}
				p.nextToken()
				value, err := p.parseNumber()
				if err != nil || value < 0 {
					return nil, fmt.Errorf("nonnegative integer token limit required")
				}
				tokens = value
				tokenLimitSet = true
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
		if len(types) == 0 {
			return nil, fmt.Errorf("model type required for %s", modelName)
		}
		if thinking && !slices.Contains(types, "chat") && !slices.Contains(types, "image2text") {
			return nil, fmt.Errorf("THINK requires chat or vision")
		}
		model := customModelDeclaration{Name: modelName, Types: types, MaxTokens: tokens}
		if thinking {
			value := true
			model.Thinking = &value
		}
		models = append(models, model)
		if p.curToken.Type != TokenComma {
			break
		}
		if len(models) == len(names) {
			return nil, fmt.Errorf("too many model configs")
		}
		p.nextToken()
	}
	if len(models) != len(names) || p.curToken.Type == TokenComma {
		return nil, fmt.Errorf("model config count must match model name count")
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
	cmd.Params["models"] = models
	return cmd, nil
}

// DROP MODEL 'name' FROM 'provider' 'instance'; also accepts explicit keywords.
func (p *Parser) parseDropInstanceModel() (*Command, error) {
	p.nextToken()
	model, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	names, err := parseModelNames(model)
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
	cmd.Params["model_names"], cmd.Params["provider_name"], cmd.Params["instance_name"] = names, provider, instance
	return cmd, nil
}
