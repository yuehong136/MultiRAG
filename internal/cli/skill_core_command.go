package cli

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"strings"
	"time"

	"multirag/internal/cli/contextengine"
)

const skillCoreUsage = `skill-core commands (ragflow-skills-v1; /api/v1/skill-core):
  spaces | create-space NAME [DESCRIPTION] | space SPACE
  rename-space SPACE NAME | delete-space SPACE
  config SPACE | set-config SPACE JSON_FILE | reindex SPACE
  search SPACE QUERY [PAGE]
Filesystem commands: ls skills/SPACE, ls skills/SPACE/SKILL,
  cat skills/SPACE/SKILL/VERSION/SKILL.md
Local package commands: install-skill SPACE DIRECTORY [--version VERSION] [--name NAME]
  uninstall-skill SPACE SKILL
Installation keeps upstream directory/version semantics. An index failure is an error
with saved files retained for retry. delete-space returns accepted deletion, not completion.
Use 'skills help' for Python asset extensions (immutable versions/activation/operations).`

func isSkillCommand(word string) bool {
	switch strings.ToLower(word) {
	case "skills", "skill-core", "install-skill", "uninstall-skill":
		return true
	}
	return false
}

func (c *CLI) executeSkillCore(command string, args []string) error {
	if c.client.ServerType == "admin" {
		return fmt.Errorf("Skills commands require user mode")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	client := &skillCoreClient{client: c.client.HTTPClient, ctx: ctx}
	provider := contextengine.NewSkillProvider(client)
	if command == "install-skill" || command == "uninstall-skill" {
		return runSkillPackageCommand(ctx, provider, command, args)
	}
	if len(args) == 0 || args[0] == "help" || args[0] == "--help" {
		fmt.Println(skillCoreUsage)
		return nil
	}
	verb, args := args[0], args[1:]
	require := func(n, m int) error {
		if len(args) < n || len(args) > m {
			return fmt.Errorf("invalid arguments for skill-core %s; use skill-core help", verb)
		}
		return nil
	}
	method, path := "GET", ""
	var body map[string]any
	switch verb {
	case "spaces":
		if err := require(0, 0); err != nil {
			return err
		}
		path = "/skills/spaces"
	case "create-space":
		if err := require(1, 2); err != nil {
			return err
		}
		description := ""
		if len(args) > 1 {
			description = args[1]
		}
		method, path, body = "POST", "/skills/spaces", map[string]any{"name": args[0], "description": description}
	case "space", "rename-space", "delete-space", "config", "set-config", "reindex", "search":
		min, max := 1, 1
		switch verb {
		case "rename-space", "set-config":
			min, max = 2, 2
		case "search":
			min, max = 2, 3
		}
		if err := require(min, max); err != nil {
			return err
		}
		if _, err := skillID(args[0]); err != nil {
			return err
		}
		id := args[0]
		path = "/skills/spaces/" + id
		switch verb {
		case "rename-space":
			method, body = "PUT", map[string]any{"name": args[1]}
		case "delete-space":
			method = "DELETE"
		case "config":
			path = "/skills/config?space_id=" + url.QueryEscape(id)
		case "set-config":
			file, err := os.Open(args[1])
			if err != nil {
				return err
			}
			defer file.Close()
			decoder := json.NewDecoder(file)
			decoder.UseNumber()
			if err = decoder.Decode(&body); err != nil {
				return err
			}
			if body == nil {
				return fmt.Errorf("config must be a JSON object")
			}
			if configured, ok := body["space_id"]; ok && configured != id {
				return fmt.Errorf("config space_id does not match command")
			}
			body["space_id"] = id
			method, path = "POST", "/skills/config"
		case "reindex":
			method, path, body = "POST", "/skills/reindex", map[string]any{"space_id": id}
		case "search":
			page := int64(1)
			var err error
			if len(args) > 2 {
				page, err = skillPositive(args[2])
				if err != nil {
					return err
				}
			}
			method, path, body = "POST", "/skills/search", map[string]any{"space_id": id, "query": args[1], "page": page, "page_size": 20}
		}
	default:
		return fmt.Errorf("unknown skill-core command %q", verb)
	}
	response, err := client.Request(method, path, true, "api", nil, body)
	if err != nil {
		return err
	}
	var envelope skillsEnvelope
	if err = json.Unmarshal(response.Body, &envelope); err != nil {
		return err
	}
	fmt.Println(string(envelope.Data))
	return nil
}

func runSkillPackageCommand(ctx context.Context, provider *contextengine.SkillProvider, command string, args []string) error {
	if len(args) == 0 || args[0] == "--help" || args[0] == "-h" {
		fmt.Println(skillCoreUsage)
		return nil
	}
	if command == "uninstall-skill" {
		if len(args) != 2 {
			return fmt.Errorf("use uninstall-skill SPACE SKILL")
		}
		if err := provider.Uninstall(ctx, args[0], args[1]); err != nil {
			return err
		}
		fmt.Println(`{"uninstalled":true}`)
		return nil
	}
	if len(args) < 2 {
		return fmt.Errorf("use install-skill SPACE DIRECTORY [--version VERSION] [--name NAME]")
	}
	space, directory := args[0], args[1]
	version, name := "", ""
	for i := 2; i < len(args); i += 2 {
		if i+1 >= len(args) {
			return fmt.Errorf("missing option value")
		}
		switch args[i] {
		case "--version":
			version = args[i+1]
		case "--name":
			name = args[i+1]
		default:
			return fmt.Errorf("unsupported option %q; only local directory installation is enabled", args[i])
		}
	}
	if err := provider.UploadSkill(ctx, directory, version, space, nil, name); err != nil {
		return err
	}
	fmt.Println(`{"installed":true,"indexed":true}`)
	return nil
}
