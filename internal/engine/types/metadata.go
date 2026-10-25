package types

import (
	"fmt"
	"strings"
)

// ValidateMetadataKeys rejects an ambiguous or empty selection before any I/O.
// Keys are literal map keys, so punctuation is allowed and is never interpolated.
func ValidateMetadataKeys(keys []string) error {
	if len(keys) == 0 || len(keys) > 100 {
		return fmt.Errorf("between 1 and 100 metadata keys are required")
	}
	seen := map[string]bool{}
	for _, key := range keys {
		if strings.TrimSpace(key) == "" || seen[key] {
			return fmt.Errorf("distinct non-empty metadata keys are required")
		}
		seen[key] = true
	}
	return nil
}
