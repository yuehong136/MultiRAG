package server

import "testing"

func TestSkillsProtocolRejectsExplicitEmpty(t *testing.T) {
	t.Setenv("SKILLS_API_PROTOCOL", "")
	if _, err := SkillsProtocol(); err == nil {
		t.Fatal("explicit empty protocol accepted")
	}
	for _, protocol := range []string{SkillsAssetsProtocol, SkillsCoreProtocol} {
		t.Setenv("SKILLS_API_PROTOCOL", protocol)
		if got, err := SkillsProtocol(); err != nil || got != protocol {
			t.Fatalf("protocol %q: %q %v", protocol, got, err)
		}
	}
}
