package handler

import (
	"bytes"
	"encoding/json"
	"fmt"
	"github.com/gin-gonic/gin"
	"io"
	"net/http"
	"unicode/utf8"
)

// bindObjectJSON rejects unknown, duplicate, null and trailing top-level values.
// Validation errors intentionally never contain request values or credentials.
func bindObjectJSON(c *gin.Context, target any) error {
	return bindObjectJSONNullable(c, target, nil)
}

// bindObjectJSONNullable permits null only for explicitly nullable fields.
func bindObjectJSONNullable(c *gin.Context, target any, nullable map[string]bool) error {
	raw, err := io.ReadAll(http.MaxBytesReader(c.Writer, c.Request.Body, 1<<20))
	if err != nil {
		return fmt.Errorf("invalid payload")
	}
	if !utf8.Valid(raw) {
		return fmt.Errorf("invalid payload")
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	token, err := dec.Token()
	if err != nil || token != json.Delim('{') {
		return fmt.Errorf("object required")
	}
	seen := map[string]bool{}
	for dec.More() {
		token, err = dec.Token()
		if err != nil {
			return fmt.Errorf("invalid payload")
		}
		key, ok := token.(string)
		if !ok || seen[key] {
			return fmt.Errorf("duplicate field")
		}
		seen[key] = true
		var value json.RawMessage
		if err = dec.Decode(&value); err != nil || (bytes.Equal(bytes.TrimSpace(value), []byte("null")) && !nullable[key]) {
			return fmt.Errorf("invalid field")
		}
	}
	if _, err = dec.Token(); err != nil {
		return fmt.Errorf("invalid payload")
	}
	if _, err = dec.Token(); err != io.EOF {
		return fmt.Errorf("trailing payload")
	}
	dec = json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err = dec.Decode(target); err != nil {
		return fmt.Errorf("invalid field")
	}
	return nil
}
