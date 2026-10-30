package service

import (
	"encoding/json"
	"fmt"
	"strings"

	"multirag/internal/tokenizer"
)

// NumTokensFromString uses the configured native tokenizer, and fails if it is
// unavailable. Nonempty text can never silently consume a zero token budget.
func NumTokensFromString(text string) (int, error) {
	if text == "" {
		return 0, nil
	}
	tokens, err := tokenizer.Tokenize(text)
	if err != nil {
		return 0, fmt.Errorf("knowledge tokenizer unavailable: %w", err)
	}
	n := len(strings.Fields(tokens))
	if n == 0 {
		return 0, fmt.Errorf("knowledge tokenizer returned no tokens")
	}
	return n, nil
}

// BuildKnowledgePrompt budgets whole JSON entries with deterministic metadata
// ordering. It returns exactly the chunks the model was allowed to see.
func BuildKnowledgePrompt(chunks []SourcedChunk, maxTokens int) (string, []SourcedChunk, error) {
	return buildKnowledgePrompt(chunks, maxTokens, NumTokensFromString)
}

func buildKnowledgePrompt(chunks []SourcedChunk, maxTokens int, count func(string) (int, error)) (string, []SourcedChunk, error) {
	selected := []SourcedChunk{}
	if maxTokens <= 0 || len(chunks) == 0 {
		return "", selected, nil
	}
	prompt := ""
	for _, chunk := range chunks {
		candidate := append(append([]SourcedChunk{}, selected...), chunk)
		raw, err := json.Marshal(candidate)
		if err != nil {
			return "", nil, err
		}
		n, err := count(string(raw))
		if err != nil {
			return "", nil, err
		}
		if n <= 0 {
			return "", nil, fmt.Errorf("invalid knowledge token count")
		}
		if n > maxTokens {
			break
		}
		prompt, selected = string(raw), candidate
	}
	return prompt, selected, nil
}
