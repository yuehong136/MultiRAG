package cli

import (
	"encoding/json"
	"fmt"
	"multirag/internal/engine/types"
	"strings"
)

func (p *Parser) parseDeleteMeta() (*Command, error) {
	p.nextToken()
	for _, expected := range []int{TokenOf, TokenDocument} {
		if p.curToken.Type != expected {
			return nil, fmt.Errorf("expected OF DOCUMENT after DELETE METADATA")
		}
		p.nextToken()
	}
	doc, err := p.parseQuotedString()
	if err != nil || strings.TrimSpace(doc) == "" {
		return nil, fmt.Errorf("non-empty document required")
	}
	cmd := NewCommand("delete_meta")
	cmd.Params["doc_id"] = doc
	p.nextToken()
	if p.curToken.Type == TokenKeys {
		p.nextToken()
		encoded, err := p.parseQuotedString()
		if err != nil {
			return nil, err
		}
		var keys []string
		if err := json.Unmarshal([]byte(encoded), &keys); err != nil {
			return nil, err
		}
		if err := types.ValidateMetadataKeys(keys); err != nil {
			return nil, err
		}
		cmd.Params["keys"] = encoded
		p.nextToken()
	}
	if err := p.finishDatasetCommand(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (c *MultiRAGClient) DeleteMeta(cmd *Command) (ResponseIf, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("USER mode required")
	}
	doc, _ := cmd.Params["doc_id"].(string)
	if strings.TrimSpace(doc) == "" {
		return nil, fmt.Errorf("document required")
	}
	payload := map[string]interface{}{"doc_id": doc}
	if raw, present := cmd.Params["keys"]; present {
		encoded, ok := raw.(string)
		if !ok {
			return nil, fmt.Errorf("keys must be a JSON array string")
		}
		var keys []string
		if err := json.Unmarshal([]byte(encoded), &keys); err != nil {
			return nil, err
		}
		if err := types.ValidateMetadataKeys(keys); err != nil {
			return nil, err
		}
		payload["keys"] = encoded
	}
	response, err := c.HTTPClient.Request("POST", "/document/delete_meta", "api", nil, payload)
	if err != nil {
		return nil, err
	}
	if err := checkAuthResponse("delete metadata", response); err != nil {
		return nil, err
	}
	var result struct {
		Data *bool `json:"data"`
	}
	if err := json.Unmarshal(response.Body, &result); err != nil || result.Data == nil || !*result.Data {
		return nil, fmt.Errorf("metadata deletion was not acknowledged")
	}
	return &SimpleResponse{Message: "Metadata deleted from " + doc}, nil
}
