package models

import (
	"bufio"
	"fmt"
	"strings"
)

// nextOpenAIStreamData preserves legacy line framing for existing providers.
// Event-framed providers join data fields according to the SSE protocol.
func nextOpenAIStreamData(scanner *bufio.Scanner, events bool) (string, bool, error) {
	var fields []string
	size := 0
	for scanner.Scan() {
		line := scanner.Text()
		if events && line == "" && len(fields) > 0 {
			return strings.Join(fields, "\n"), true, nil
		}
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		value := strings.TrimPrefix(line, "data:")
		if !events {
			value = strings.TrimSpace(value)
			if value != "" {
				return value, true, nil
			}
			continue
		}
		value = strings.TrimPrefix(value, " ")
		size += len(value) + 1
		if size > 4*1024*1024 {
			return "", false, fmt.Errorf("model SSE event exceeds size limit")
		}
		fields = append(fields, value)
	}
	if err := scanner.Err(); err != nil {
		return "", false, err
	}
	if len(fields) > 0 {
		return strings.Join(fields, "\n"), true, nil
	}
	return "", false, nil
}
