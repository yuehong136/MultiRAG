package server

import (
	"fmt"
	"os"
)

const SkillsAssetsProtocol = "multirag-assets-v1"
const SkillsCoreProtocol = "ragflow-skills-v1"

func SkillsProtocol() (string, error) {
	value, configured := os.LookupEnv("SKILLS_API_PROTOCOL")
	if !configured {
		value = SkillsAssetsProtocol
	}
	if value != SkillsAssetsProtocol && value != SkillsCoreProtocol {
		return "", fmt.Errorf("invalid SKILLS_API_PROTOCOL %q", value)
	}
	return value, nil
}
