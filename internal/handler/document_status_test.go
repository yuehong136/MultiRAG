package handler

import (
	"encoding/json"
	"testing"
)

func TestParseDocumentStatusStrict(t *testing.T) {
	for _, value := range []string{"0", "1", `"0"`, `"1"`} {
		if _, err := parseDocumentStatus(json.RawMessage(value)); err != nil {
			t.Fatalf("valid %s: %v", value, err)
		}
	}
	for _, value := range []string{"0.0", "1.0", "0.5", "true", "false", "null", "2", `"01"`, `" 0"`, "[]", "{}", ""} {
		if _, err := parseDocumentStatus(json.RawMessage(value)); err == nil {
			t.Fatalf("invalid status accepted: %s", value)
		}
	}
}
