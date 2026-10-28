package models

import (
	"regexp"
	"strconv"
	"strings"
)

var claudeVersionPattern = regexp.MustCompile(`^claude-(?:opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?:-|:|$)`)
var claudeLegacyVersionPattern = regexp.MustCompile(`^claude-(\d+)(?:-(\d{1,2}))?-(?:opus|sonnet|haiku)(?:-|:|$)`)

// Canonical model identities may carry a provider or Bedrock profile prefix.
func canonicalClaudeName(name string) string {
	name = strings.ToLower(strings.TrimSpace(name))
	name = strings.TrimPrefix(name, "anthropic/")
	for _, prefix := range []string{"us.", "eu.", "global."} {
		name = strings.TrimPrefix(name, prefix)
	}
	return strings.TrimPrefix(name, "anthropic.")
}

// Apply the model's sampling restrictions to native Anthropic and Converse
// fields. This edits only the newly constructed request, never caller config.
func applyClaudeSamplingPolicy(name string, body map[string]any) {
	name = canonicalClaudeName(name)
	if !strings.HasPrefix(name, "claude-") {
		return
	}
	for _, marker := range []string{"claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-sonnet-5", "claude-fable", "claude-mythos"} {
		if name == marker || strings.HasPrefix(name, marker+"-") || strings.HasPrefix(name, marker+":") {
			for _, key := range []string{"temperature", "top_p", "top_k", "topP", "topK"} {
				delete(body, key)
			}
			return
		}
	}
	match := claudeVersionPattern.FindStringSubmatch(name)
	if match == nil {
		match = claudeLegacyVersionPattern.FindStringSubmatch(name)
	}
	if match != nil {
		major, _ := strconv.Atoi(match[1])
		minor, _ := strconv.Atoi(match[2])
		if major < 4 || major == 4 && minor < 1 {
			return
		}
	}
	if _, ok := body["temperature"]; ok {
		delete(body, "top_p")
		delete(body, "topP")
	}
}
