package models

import (
	"fmt"
	"math"
	"strings"
)

// validateHostedGeneration rejects invalid numeric options before provider I/O.
// Explicit zero remains distinct from an absent field.
func validateHostedGeneration(config *ChatConfig) error {
	if config == nil {
		return nil
	}
	if config.MaxTokens != nil && *config.MaxTokens < 0 {
		return fmt.Errorf("max_tokens must be non-negative")
	}
	if config.Temperature != nil && (math.IsNaN(*config.Temperature) || math.IsInf(*config.Temperature, 0) || *config.Temperature < 0 || *config.Temperature > 2) {
		return fmt.Errorf("temperature must be finite and between 0 and 2")
	}
	if config.TopP != nil && (math.IsNaN(*config.TopP) || math.IsInf(*config.TopP, 0) || *config.TopP < 0 || *config.TopP > 1) {
		return fmt.Errorf("top_p must be finite and between 0 and 1")
	}
	return nil
}

func requireSpeechVoice(body map[string]any) error {
	voice, ok := body["voice"].(string)
	if !ok || strings.TrimSpace(voice) == "" {
		return fmt.Errorf("voice is required")
	}
	return nil
}
