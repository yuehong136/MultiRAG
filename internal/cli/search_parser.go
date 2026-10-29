package cli

import (
	"fmt"
	"strconv"
	"strings"
)

// parseSearchCommand parses the dataset search grammar. Context Engine search
// continues to use its own flag grammar in cli.go.
func (p *Parser) parseSearchCommand() (*Command, error) {
	p.nextToken() // consume SEARCH
	if p.curToken.Type == TokenIllegal && p.curToken.Value == "-" {
		dashes := 0
		for p.curToken.Type == TokenIllegal && p.curToken.Value == "-" {
			dashes++
			p.nextToken()
		}
		flag := strings.ToLower(p.curToken.Value)
		if !((dashes == 1 && flag == "h") || (dashes == 2 && flag == "help")) {
			return nil, fmt.Errorf("expected -h or --help")
		}
		p.nextToken()
		if p.curToken.Type == TokenSemicolon {
			p.nextToken()
		}
		if p.curToken.Type != TokenEOF {
			return nil, fmt.Errorf("unexpected search help input: %s", p.curToken.Value)
		}
		return NewCommand("search_help"), nil
	}
	question, err := p.parseQuotedString()
	if err != nil || strings.TrimSpace(question) == "" {
		return nil, fmt.Errorf("a non-empty quoted search question is required")
	}
	p.nextToken()
	for _, expected := range []int{TokenOn, TokenDatasets} {
		if p.curToken.Type != expected {
			return nil, fmt.Errorf("expected ON DATASETS after search question")
		}
		p.nextToken()
	}
	var names []string
	for p.curToken.Type == TokenQuotedString {
		value, err := p.parseQuotedString()
		if err != nil {
			return nil, err
		}
		names = append(names, value)
		p.nextToken()
	}
	datasetNames, err := parseSearchDatasetNames(strings.Join(names, ","))
	if err != nil {
		return nil, err
	}
	options := make(map[string]interface{})
	if strings.EqualFold(p.curToken.Value, "with") {
		p.nextToken()
		if p.curToken.Type == TokenSemicolon || p.curToken.Type == TokenEOF {
			return nil, fmt.Errorf("WITH requires at least one search option")
		}
		for p.curToken.Type != TokenSemicolon && p.curToken.Type != TokenEOF {
			if p.curToken.Type != TokenIdentifier && !isKeyword(p.curToken.Type) {
				return nil, fmt.Errorf("expected an unquoted search option name")
			}
			name, err := searchOptionName(p.curToken.Value)
			if err != nil {
				return nil, err
			}
			if _, exists := options[name]; exists {
				return nil, fmt.Errorf("duplicate search option %q", name)
			}
			p.nextToken()
			var value interface{}
			switch name {
			case "top_k", "page", "size":
				value, err = p.parseNumber()
				if err == nil {
					p.nextToken()
				}
			case "similarity_threshold", "vector_similarity_weight":
				value, err = p.parseFloat()
				if err == nil {
					p.nextToken()
				}
			case "keyword", "use_kg":
				switch strings.ToLower(p.curToken.Value) {
				case "true":
					value = true
				case "false":
					value = false
				default:
					err = fmt.Errorf("search option %q requires true or false", name)
				}
				if p.curToken.Type == TokenQuotedString {
					err = fmt.Errorf("search option %q requires an unquoted boolean", name)
				}
				if err == nil {
					p.nextToken()
				}
			case "cross_languages", "doc_ids":
				value, err = p.parseSearchStringList()
			case "tenant_rerank_id":
				if p.curToken.Type != TokenNumber {
					err = fmt.Errorf("tenant_rerank_id requires a positive integer")
				} else {
					value, err = strconv.ParseInt(p.curToken.Value, 10, 64)
					if err == nil {
						p.nextToken()
					}
				}
			default:
				value, err = p.parseQuotedString()
				if err == nil {
					p.nextToken()
				}
			}
			if err != nil {
				return nil, fmt.Errorf("invalid search option %q: %w", name, err)
			}
			options[name] = value
		}
	}
	if _, err := searchOptionsPayload(options); err != nil {
		return nil, err
	}
	if err := p.finishDatasetCommand(); err != nil {
		return nil, err
	}
	cmd := NewCommand("search_on_datasets")
	cmd.Params["question"] = question
	cmd.Params["datasets"] = strings.Join(datasetNames, ",")
	cmd.Params["options"] = options
	return cmd, nil
}

func (p *Parser) parseSearchStringList() ([]string, error) {
	if p.curToken.Type != TokenLBracket {
		return nil, fmt.Errorf("expected a bracketed list of quoted strings")
	}
	p.nextToken()
	values := make([]string, 0)
	if p.curToken.Type == TokenRBracket {
		p.nextToken()
		return values, nil
	}
	for {
		value, err := p.parseQuotedString()
		if err != nil {
			return nil, err
		}
		values = append(values, value)
		p.nextToken()
		if p.curToken.Type == TokenRBracket {
			p.nextToken()
			return values, nil
		}
		if p.curToken.Type != TokenComma {
			return nil, fmt.Errorf("expected ',' or ']' after list item")
		}
		p.nextToken()
	}
}
