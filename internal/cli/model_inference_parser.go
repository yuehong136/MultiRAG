package cli

import "fmt"

func (p *Parser) parseInferenceCommand(rerank bool) (*Command, error) {
	p.nextToken()
	expected := TokenText
	if rerank {
		expected = TokenQuery
	}
	if p.curToken.Type != expected {
		return nil, fmt.Errorf("expected TEXT or QUERY")
	}
	p.nextToken()
	cmd := NewCommand("embed_user_text")
	if rerank {
		cmd.Type = "rerank_user_document"
		query, err := p.parseQuotedString()
		if err != nil {
			return nil, err
		}
		cmd.Params["query"] = query
		p.nextToken()
		if p.curToken.Type != TokenDocument {
			return nil, fmt.Errorf("expected DOCUMENT")
		}
		p.nextToken()
	}
	values := []string{}
	for p.curToken.Type == TokenQuotedString {
		value, err := p.parseQuotedString()
		if err != nil {
			return nil, err
		}
		values = append(values, value)
		p.nextToken()
	}
	if len(values) == 0 {
		return nil, fmt.Errorf("at least one text or document required")
	}
	field := "texts"
	if rerank {
		field = "documents"
	}
	cmd.Params[field] = values
	if p.curToken.Type != TokenWith {
		return nil, fmt.Errorf("expected WITH")
	}
	p.nextToken()
	name, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	cmd.Params["composite_model_name"] = name
	p.nextToken()
	option := TokenDimension
	field = "dimension"
	if rerank {
		option = TokenTop
		field = "top_n"
	}
	if p.curToken.Type != option {
		return nil, fmt.Errorf("expected DIMENSION or TOP")
	}
	p.nextToken()
	number, err := p.parseNumber()
	if err != nil || number < 0 {
		return nil, fmt.Errorf("expected non-negative integer")
	}
	cmd.Params[field] = number
	p.nextToken()
	if p.curToken.Type != TokenSemicolon {
		return nil, fmt.Errorf("expected semicolon")
	}
	if err := p.expectSemicolon(); err != nil {
		return nil, err
	}
	return cmd, nil
}
