package handler

import (
	"fmt"
	"strings"
)

func validateModelSelection(id *int64, provider, instance, name string) error {
	if id != nil {
		if *id <= 0 || provider != "" || instance != "" || name != "" {
			return fmt.Errorf("select a positive numeric model_id exclusively")
		}
		return nil
	}
	for _, part := range []string{provider, instance, name} {
		if strings.TrimSpace(part) == "" || strings.Contains(part, "@") {
			return fmt.Errorf("provider, instance and model are required")
		}
	}
	return nil
}
