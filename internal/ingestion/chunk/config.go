package chunk

import (
	"bytes"
	"encoding/json"
	"fmt"
)

// decodeConfig rejects misspelled options instead of silently changing the plan.
func decodeConfig(config map[string]any, target any) error {
	copy := make(map[string]any, len(config))
	for key, value := range config {
		if key != "operator" {
			copy[key] = value
		}
	}
	raw, err := json.Marshal(copy)
	if err != nil {
		return err
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	if err := d.Decode(target); err != nil {
		return fmt.Errorf("invalid configuration: %w", err)
	}
	return nil
}
