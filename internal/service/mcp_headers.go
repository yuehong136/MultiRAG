package service

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
)

// MCPHeaders distinguishes explicit empty header strings from JSON null.
// The standard map[string]string decoder silently turns null into "".
type MCPHeaders map[string]string

func (headers *MCPHeaders) UnmarshalJSON(raw []byte) error {
	invalid := errors.New("MCP headers must be an object of strings.")
	decoder := json.NewDecoder(bytes.NewReader(raw))
	token, err := decoder.Token()
	if err != nil || token != json.Delim('{') {
		return invalid
	}
	parsed := MCPHeaders{}
	for decoder.More() {
		token, err = decoder.Token()
		if err != nil {
			return invalid
		}
		key, ok := token.(string)
		if !ok {
			return invalid
		}
		if _, exists := parsed[key]; exists || len(parsed) >= 64 {
			return invalid
		}
		var value any
		if decoder.Decode(&value) != nil {
			return invalid
		}
		text, ok := value.(string)
		if !ok {
			return invalid
		}
		parsed[key] = text
	}
	if token, err = decoder.Token(); err != nil || token != json.Delim('}') {
		return invalid
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		return invalid
	}
	*headers = parsed
	return nil
}
