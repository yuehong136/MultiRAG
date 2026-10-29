package service

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
)

// decodeMetadataObject preserves exact JSON numbers and rejects trailing data.
func decodeMetadataObject(raw []byte, object *map[string]interface{}) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(object); err != nil {
		return err
	}
	if err := decoder.Decode(new(interface{})); err != io.EOF {
		return fmt.Errorf("document metadata has trailing data")
	}
	return nil
}

// decodeDocumentMetadata is strict at document-list and reference boundaries.
func decodeDocumentMetadata(value any) (map[string]any, error) {
	empty := func() map[string]any { return map[string]any{} }
	decode := func(raw []byte) (map[string]any, error) {
		var object map[string]any
		if err := decodeMetadataObject(raw, &object); err != nil {
			return nil, fmt.Errorf("invalid document metadata JSON object")
		}
		if object == nil {
			object = empty()
		}
		return object, nil
	}
	switch data := value.(type) {
	case nil:
		return empty(), nil
	case map[string]any:
		if _, err := json.Marshal(data); err != nil {
			return nil, fmt.Errorf("invalid document metadata object")
		}
		return data, nil
	case string:
		return decode([]byte(data))
	case []byte:
		if len(data) == 0 {
			return empty(), nil
		}
		if object, err := decode(data); err == nil {
			return object, nil
		}
		merged := empty()
		for len(data) > 0 {
			if len(data) < 4 {
				return nil, fmt.Errorf("truncated document metadata length prefix")
			}
			length := uint64(binary.LittleEndian.Uint32(data[:4]))
			data = data[4:]
			if length == 0 || length > uint64(len(data)) {
				return nil, fmt.Errorf("invalid document metadata length prefix")
			}
			object, err := decode(bytes.Clone(data[:int(length)]))
			if err != nil {
				return nil, err
			}
			data = data[int(length):]
			for key, value := range object {
				if previous, ok := merged[key]; ok {
					values := []any{}
					for _, item := range []any{previous, value} {
						if list, ok := item.([]any); ok {
							values = append(values, list...)
						} else {
							values = append(values, item)
						}
					}
					merged[key] = values
				} else {
					merged[key] = value
				}
			}
		}
		return merged, nil
	default:
		return nil, fmt.Errorf("document metadata must be an object")
	}
}
