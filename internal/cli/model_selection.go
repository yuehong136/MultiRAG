package cli

import (
	"fmt"
	"strconv"
	"strings"
)

func modelSelectionPayload(raw string) (map[string]any, error) {
	if id, err := strconv.ParseInt(raw, 10, 64); err == nil && id > 0 && strconv.FormatInt(id, 10) == raw {
		return map[string]any{"model_id": id}, nil
	}
	parts := strings.Split(raw, "@")
	if len(parts) != 3 {
		return nil, fmt.Errorf("model must be a positive numeric ID or model@instance@provider")
	}
	for _, part := range parts {
		if part == "" || strings.TrimSpace(part) != part {
			return nil, fmt.Errorf("invalid model identifier")
		}
	}
	return map[string]any{"model_name": parts[0], "instance_name": parts[1], "provider_name": parts[2]}, nil
}
