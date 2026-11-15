package cli

import (
	"testing"
)

func TestCustomModelZeroTokenLimit(t *testing.T) {
	cmd, err := NewParser(`ADD MODEL 'zero' TO PROVIDER 'vllm' INSTANCE 'local' WITH TOKENS 0 CHAT;`).Parse(APIMode)
	if err != nil || cmd.Params["models"].([]map[string]any)[0]["max_tokens"] != 0 {
		t.Fatalf("zero token limit: %v %v", cmd, err)
	}
}
