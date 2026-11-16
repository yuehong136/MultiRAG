package cli

import (
	"reflect"
	"testing"
)

func TestFilesystemParserRetainsPathsAndOptions(t *testing.T) {
	for input, expected := range map[string][]string{
		`ls skills`:                                                     {"ls", "skills"},
		`ls skills/space --limit 2;`:                                    {"ls", "skills/space", "--limit", "2"},
		`cat "skills/space/中文 file/SKILL.md"`:                           {"cat", "skills/space/中文 file/SKILL.md"},
		`search --query "orange guide" --dirs skills/space`:             {"search", "--query", "orange guide", "--dirs", "skills/space"},
		`install-skill space "local skill with spaces" --version 1.0.0`: {"install-skill", "space", "local skill with spaces", "--version", "1.0.0"},
		`uninstall-skill space orange`:                                  {"uninstall-skill", "space", "orange"},
	} {
		t.Run(input, func(t *testing.T) {
			command, err := NewParser(input).Parse(APIMode)
			if err != nil || command == nil || command.Type != "file_system_command" {
				t.Fatalf("filesystem command rejected: %#v, %v", command, err)
			}
			parsedArgs := parseFilesystemArgs(command.Params["command"].(string))

			if !reflect.DeepEqual(parsedArgs, expected) {
				t.Fatalf("filesystem arguments changed: %q != %q", parsedArgs, expected)
			}
		})
	}
}

func TestFilesystemParserKeepsCommandBoundaries(t *testing.T) {
	for _, input := range []string{
		"ls skills; LIST DATASETS;", "cat skills/a; trailing", "install-skill space local; ignored",
		`cat "unterminated`, `install-skill space 'unterminated`,
		"ls skills\x00ignored", "skill-core space old", "skills create-space old",
	} {
		if command, err := NewParser(input).Parse(APIMode); err == nil {
			t.Fatalf("invalid or retired command accepted: %q: %#v", input, command)
		}
	}
	for _, input := range []string{"install-skill space local", "uninstall-skill space local"} {
		if command, err := NewParser(input).Parse(AdminMode); err == nil {
			t.Fatalf("filesystem mutation accepted in admin mode: %q: %#v", input, command)
		}
	}
}
