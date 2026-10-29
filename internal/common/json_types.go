package common

import (
	"bytes"
	"encoding/json"
	"fmt"
)

// StringSlice accepts a JSON string or string array. Omission leaves nil, an
// empty array stays non-nil, and null is rejected rather than widening scope.
type StringSlice []string

func (s *StringSlice) UnmarshalJSON(raw []byte) error {
	raw = bytes.TrimSpace(raw)
	if len(raw) == 0 || bytes.Equal(raw, []byte("null")) {
		return fmt.Errorf("string or string array required")
	}
	var values []string
	if raw[0] == '"' {
		var value string
		if err := json.Unmarshal(raw, &value); err != nil {
			return err
		}
		values = []string{value}
	} else {
		// Decode elements separately: encoding/json otherwise accepts null
		// elements as empty strings when decoding directly to []string.
		var elements []json.RawMessage
		if err := json.Unmarshal(raw, &elements); err != nil {
			return err
		}
		values = make([]string, len(elements))
		for i, element := range elements {
			if bytes.Equal(bytes.TrimSpace(element), []byte("null")) {
				return fmt.Errorf("string array required")
			}
			if err := json.Unmarshal(element, &values[i]); err != nil {
				return err
			}
		}
	}
	*s = values
	return nil
}
