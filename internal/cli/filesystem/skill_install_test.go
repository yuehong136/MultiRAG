package filesystem

import (
	"context"
	"multirag/internal/cli/filesystem/skill_hub/source"
	"testing"
)

func TestSkillInstallRejectsTraversalBeforeWriting(t *testing.T) {
	command := &SkillInstallCommand{}
	for _, name := range []string{"../escape", "/escape", "a/b", "a\\b", "..", ""} {
		bundle := &source.SkillBundle{Name: name, Metadata: &source.SkillMetadata{}, Files: map[string][]byte{"SKILL.md": []byte("safe")}}
		if err := command.installSkill(context.Background(), "space", bundle, false); err == nil {
			t.Fatal("invalid skill name", name)
		}
	}
	for _, path := range []string{"../escape", "/escape", "a/../escape", "a\\escape", "a\x00b", "e\u0301.txt", "a\u200bb", "C:/absolute"} {
		bundle := &source.SkillBundle{Name: "safe", Metadata: &source.SkillMetadata{}, Files: map[string][]byte{path: []byte("safe")}}
		if err := command.installSkill(context.Background(), "space", bundle, false); err == nil {
			t.Fatal("invalid skill path", path)
		}
	}
	if err := (&SkillUninstallCommand{}).uninstallSkill(context.Background(), "space", "skill"); err == nil {
		t.Fatal("unavailable uninstall reported success")
	}
}

func TestCanonicalSkillFrontmatterTypes(t *testing.T) {
	for _, content := range []string{"---\nname: orange\ndescription: 123\n---\nbody", "---\nname: orange\ntags: [1]\n---\nbody", "---\nname: orange\ndescription: &value okay\ntags: [*value]\n---\nbody", "---\nname: orange\n---garbage"} {
		if _, err := parseFrontmatter(content); err == nil {
			t.Error("unsafe frontmatter accepted", content)
		}
	}
	metadata, err := parseFrontmatter("---\nname: orange\ndescription: |\n  中文说明\n  multiline\ntags: [fruit]\n---\nbody")
	if err != nil || metadata.Name != "orange" || metadata.Description != "中文说明\nmultiline\n" || len(metadata.Tags) != 1 {
		t.Fatal(metadata, err)
	}
}
