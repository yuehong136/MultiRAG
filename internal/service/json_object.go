package service

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
)

func strictJSONObject(raw []byte) (map[string]json.RawMessage, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	start, err := decoder.Token()
	if err != nil || start != json.Delim('{') {
		return nil, errors.New("invalid JSON object")
	}
	result := map[string]json.RawMessage{}
	for decoder.More() {
		key, err := decoder.Token()
		if err != nil {
			return nil, errors.New("invalid JSON object")
		}
		name, ok := key.(string)
		if !ok {
			return nil, errors.New("invalid JSON object")
		}
		if _, exists := result[name]; exists {
			return nil, errors.New("invalid JSON object")
		}
		var value json.RawMessage
		if decoder.Decode(&value) != nil {
			return nil, errors.New("invalid JSON object")
		}
		result[name] = value
	}
	if _, err = decoder.Token(); err != nil {
		return nil, errors.New("invalid JSON object")
	}
	if _, err = decoder.Token(); !errors.Is(err, io.EOF) {
		return nil, errors.New("invalid JSON object")
	}
	return result, nil
}
