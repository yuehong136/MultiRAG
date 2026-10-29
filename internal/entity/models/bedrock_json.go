package models

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"strings"
	"unicode"
	"unicode/utf8"
)

// encoding/json accepts duplicate and case-insensitive struct members. Reject
// those ambiguities before decoding credentials or embedding provider data.
func decodeBedrockJSON(reader io.Reader, target any) error {
	raw, err := io.ReadAll(io.LimitReader(reader, MaxMediaBytes+1))
	if err != nil {
		return err
	}
	if len(raw) > MaxMediaBytes || !utf8.Valid(raw) {
		return fmt.Errorf("bedrock: oversized JSON response")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := bedrockJSONValue(decoder, 0); err != nil {
		return fmt.Errorf("bedrock: ambiguous or invalid JSON")
	}
	if _, err := decoder.Token(); err != io.EOF {
		return fmt.Errorf("bedrock: trailing JSON data")
	}
	if err := json.Unmarshal(raw, target); err != nil {
		return fmt.Errorf("bedrock: invalid JSON schema")
	}
	return nil
}

func bedrockJSONValue(decoder *json.Decoder, depth int) error {
	if depth > 128 {
		return fmt.Errorf("JSON depth exceeded")
	}
	token, err := decoder.Token()
	if err != nil {
		return err
	}
	switch token {
	case json.Delim('{'):
		seen := map[string]bool{}
		for decoder.More() {
			member, err := decoder.Token()
			name, ok := member.(string)
			if err != nil || !ok {
				return fmt.Errorf("invalid JSON member")
			}
			folded := bedrockJSONMember(name)
			if seen[folded] {
				return fmt.Errorf("duplicate JSON member")
			}
			seen[folded] = true
			if err := bedrockJSONValue(decoder, depth+1); err != nil {
				return err
			}
		}
		if end, err := decoder.Token(); err != nil || end != json.Delim('}') {
			return fmt.Errorf("invalid JSON object")
		}
	case json.Delim('['):
		for decoder.More() {
			if err := bedrockJSONValue(decoder, depth+1); err != nil {
				return err
			}
		}
		if end, err := decoder.Token(); err != nil || end != json.Delim(']') {
			return fmt.Errorf("invalid JSON array")
		}
	case json.Delim('}'), json.Delim(']'):
		return fmt.Errorf("unexpected JSON delimiter")
	}
	return nil
}

// Match encoding/json's Unicode simple-fold equivalence, including escaped keys.
func bedrockJSONMember(name string) string {
	var folded strings.Builder
	for _, value := range name {
		minimum := value
		for next := unicode.SimpleFold(value); next != value; next = unicode.SimpleFold(next) {
			if next < minimum {
				minimum = next
			}
		}
		folded.WriteRune(minimum)
	}
	return folded.String()
}

// Compute JSON string size without allocating an escaped copy of the input.
func bedrockJSONStringSize(text string, limit int) (int, bool) {
	size := 2 // surrounding quotes
	for _, value := range text {
		switch {
		case value == '\b' || value == '\f' || value == '\n' || value == '\r' || value == '\t' || value == '\\' || value == '"':
			size += 2
		case value < 0x20 || value == '<' || value == '>' || value == '&' || value == '\u2028' || value == '\u2029':
			size += 6
		case value < 0x80:
			size++
		case value < 0x800:
			size += 2
		case value < 0x10000:
			size += 3
		default:
			size += 4
		}
		if size > limit {
			return size, false
		}
	}
	return size, size <= limit
}
