package handler

import (
	"bytes"
	"encoding/json"
	"errors"
	"github.com/gin-gonic/gin"
	"io"
)

// objectRequest rejects duplicate keys, trailing values and unbounded bodies.
func objectRequest(c *gin.Context) (map[string]json.RawMessage, error) {
	const limit = 1 << 20
	raw, err := io.ReadAll(io.LimitReader(c.Request.Body, limit+1))
	if err != nil || len(raw) > limit {
		return nil, errors.New("invalid request body")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	token, err := decoder.Token()
	if err != nil || token != json.Delim('{') {
		return nil, errors.New("expected object")
	}
	fields := map[string]json.RawMessage{}
	for decoder.More() {
		token, err = decoder.Token()
		if err != nil {
			return nil, err
		}
		key, ok := token.(string)
		if !ok {
			return nil, errors.New("invalid key")
		}
		if _, exists := fields[key]; exists {
			return nil, errors.New("duplicate field")
		}
		var value json.RawMessage
		if decoder.Decode(&value) != nil {
			return nil, errors.New("invalid field")
		}
		fields[key] = value
	}
	if _, err = decoder.Token(); err != nil {
		return nil, err
	}
	if _, err = decoder.Token(); !errors.Is(err, io.EOF) {
		return nil, errors.New("trailing request")
	}
	return fields, nil
}

func bindObjectRequest(c *gin.Context, target any) error {
	fields, err := objectRequest(c)
	if err != nil {
		return err
	}
	raw, err := json.Marshal(fields)
	if err != nil {
		return err
	}
	return json.Unmarshal(raw, target)
}
