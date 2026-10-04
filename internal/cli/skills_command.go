package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
)

const skillsUsage = `skills commands (IDs are immutable; output is JSON):
  capabilities | models | spaces [page]
  create-space NAME [DESCRIPTION] | space SPACE
  rename-space SPACE REVISION NAME | delete-space SPACE [--key KEY]
  list SPACE [page] | show SPACE SKILL
  install SPACE DIRECTORY_OR_ZIP NAME VERSION [--activate] [--key KEY]
  activate SPACE SKILL VERSION_OR_null REVISION [--key KEY]
  delete-version SPACE VERSION [--key KEY] | delete-skill SPACE SKILL [--key KEY]
  delete-spaces SPACE... [--key KEY] | delete-skills SPACE SKILL... [--key KEY]
  files SPACE VERSION | file SPACE VERSION PATH OUTPUT | download SPACE VERSION OUTPUT
  config SPACE | set-config SPACE JSON_FILE | reindex SPACE [--key KEY]
  search SPACE QUERY [keyword|vector|hybrid] [page]
  operation OPERATION | wait OPERATION | retry OPERATION [--key KEY]
Long operations return accepted operation IDs, not completed success. 'wait' checks terminal
state for up to 5 minutes and fails on partial/failed. Reuse --key after an uncertain request.
Files are never overwritten. JSON config includes revision and decimal-string model IDs.`

var skillIDPattern = regexp.MustCompile(`^[0-9a-f]{32}$`)

func skillID(value string) (string, error) {
	if !skillIDPattern.MatchString(value) {
		return "", fmt.Errorf("expected a 32-character resource ID")
	}
	return value, nil
}

func skillPositive(value string) (int64, error) {
	number, err := strconv.ParseInt(value, 10, 64)
	if err != nil || number < 1 {
		return 0, fmt.Errorf("expected a positive integer")
	}
	return number, nil
}

func (c *CLI) executeSkills(args []string) error {
	if c.client.ServerType == "admin" {
		return fmt.Errorf("Skills commands require user mode")
	}
	if len(args) == 0 || args[0] == "help" {
		fmt.Println(skillsUsage)
		return nil
	}
	key := ""
	activate := false
	values := []string{}
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--key":
			if i+1 >= len(args) || key != "" {
				return fmt.Errorf("--key requires one value")
			}
			i++
			key = args[i]
			if len(key) < 1 || len(key) > 128 {
				return fmt.Errorf("invalid operation key")
			}
			for _, char := range key {
				if char < 33 || char > 126 {
					return fmt.Errorf("invalid operation key")
				}
			}
		case "--activate":
			activate = true
		default:
			values = append(values, args[i])
		}
	}
	if len(values) == 0 {
		return fmt.Errorf("missing Skills command")
	}
	command, args := values[0], values[1:]
	if activate && command != "install" {
		return fmt.Errorf("--activate only applies to install")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	httpClient := c.client.HTTPClient
	method, path, expected := "GET", "", 200
	var body any
	var data []byte
	var err error
	require := func(minimum, maximum int) error {
		if len(args) < minimum || len(args) > maximum {
			return fmt.Errorf("invalid arguments for skills %s; use skills help", command)
		}
		return nil
	}
	spacePath := func() (string, error) {
		if len(args) == 0 {
			return "", fmt.Errorf("missing space ID")
		}
		id, err := skillID(args[0])
		return "/spaces/" + id, err
	}
	resourcePath := func(kind string) (string, error) {
		base, err := spacePath()
		if err != nil {
			return "", err
		}
		if len(args) < 2 {
			return "", fmt.Errorf("missing resource ID")
		}
		id, err := skillID(args[1])
		return base + "/" + kind + "/" + id, err
	}
	operationKey := func() string {
		if key == "" {
			key = uuid.NewString()
		}
		fmt.Fprintln(os.Stderr, "Skills operation key:", key)
		return key
	}
	switch command {
	case "capabilities", "models":
		if err = require(0, 0); err != nil {
			return err
		}
		path = "/" + command
	case "spaces":
		if err = require(0, 1); err != nil {
			return err
		}
		path = "/spaces"
		if len(args) == 1 {
			page, e := skillPositive(args[0])
			if e != nil {
				return e
			}
			path += fmt.Sprintf("?page=%d", page)
		}
	case "create-space":
		if err = require(1, 2); err != nil {
			return err
		}
		description := ""
		if len(args) == 2 {
			description = args[1]
		}
		method, path, body = "POST", "/spaces", map[string]any{"name": args[0], "description": description}
	case "space", "config", "reindex", "delete-space", "rename-space", "set-config", "list", "search":
		minimum, maximum := 1, 1
		switch command {
		case "rename-space":
			minimum, maximum = 3, 3
		case "set-config":
			minimum, maximum = 2, 2
		case "list":
			maximum = 2
		case "search":
			minimum, maximum = 2, 4
		}
		if err = require(minimum, maximum); err != nil {
			return err
		}
		path, err = spacePath()
		if err != nil {
			return err
		}
		switch command {
		case "config":
			path += "/config"
		case "set-config":
			method, path = "PATCH", path+"/config"
			file, e := os.Open(args[1])
			if e != nil {
				return e
			}
			defer file.Close()
			decoder := json.NewDecoder(file)
			decoder.UseNumber()
			if e = decoder.Decode(&body); e != nil {
				return e
			}
		case "rename-space":
			revision, e := skillPositive(args[1])
			if e != nil {
				return e
			}
			method, body = "PATCH", map[string]any{"name": args[2], "revision": revision}
		case "reindex":
			method, path, expected = "POST", path+"/reindex", 202
		case "delete-space":
			method, expected = "DELETE", 202
		case "list":
			path += "/skills"
			if len(args) == 2 {
				page, e := skillPositive(args[1])
				if e != nil {
					return e
				}
				path += fmt.Sprintf("?page=%d", page)
			}
		case "search":
			mode, page := "hybrid", int64(1)
			if len(args) > 2 {
				mode = args[2]
			}
			if len(args) > 3 {
				page, err = skillPositive(args[3])
				if err != nil {
					return err
				}
			}
			if mode != "keyword" && mode != "vector" && mode != "hybrid" {
				return fmt.Errorf("invalid search mode")
			}
			method, path, body = "POST", path+"/search", map[string]any{"query": args[1], "mode": mode, "page": page, "page_size": 20}
		}
	case "show", "files", "delete-skill", "delete-version", "activate", "download", "file":
		n := 2
		switch command {
		case "activate", "file":
			n = 4
		case "download":
			n = 3
		}
		if err = require(n, n); err != nil {
			return err
		}
		kind := "versions"
		if command == "show" || command == "delete-skill" || command == "activate" {
			kind = "skills"
		}
		path, err = resourcePath(kind)
		if err != nil {
			return err
		}
		switch command {
		case "files":
			path += "/files"
		case "delete-skill", "delete-version":
			method, expected = "DELETE", 202
		case "activate":
			var version any
			if args[2] != "null" {
				version, err = skillID(args[2])
				if err != nil {
					return err
				}
			}
			revision, e := skillPositive(args[3])
			if e != nil {
				return e
			}
			method, path, expected, body = "PUT", path+"/active-version", 202, map[string]any{"version_id": version, "revision": revision}
		case "download", "file":
			output := args[2]
			path += "/download"
			if command == "file" {
				output = args[3]
				path = strings.TrimSuffix(path, "/download") + "/file?path=" + url.QueryEscape(args[2])
			}
			data, err = httpClient.skillsRequest(ctx, "GET", path, "", "", nil, 200, true)
			if err != nil {
				return err
			}
			return saveSkillDownload(output, data)
		}
	case "delete-spaces", "delete-skills":
		offset := 0
		path = "/spaces/delete"
		if command == "delete-skills" {
			offset = 1
			base, e := spacePath()
			if e != nil {
				return e
			}
			path = base + "/skills/delete"
		}
		if err = require(offset+1, offset+100); err != nil {
			return err
		}
		for _, id := range args[offset:] {
			if _, err = skillID(id); err != nil {
				return err
			}
		}
		method, expected, body = "POST", 202, map[string]any{"ids": args[offset:]}
	case "operation", "retry", "wait":
		if err = require(1, 1); err != nil {
			return err
		}
		id, e := skillID(args[0])
		if e != nil {
			return e
		}
		path = "/operations/" + id
		if command == "wait" {
			data, err = httpClient.waitSkillOperation(ctx, id)
			if len(data) > 0 {
				printSkillsData(data)
			}
			return err
		}
		if command == "retry" {
			method, path, expected = "POST", path+"/retry", 202
		}
	case "install":
		if err = require(4, 4); err != nil {
			return err
		}
		path, err = spacePath()
		if err != nil {
			return err
		}
		packageBody, contentType, e := buildSkillUpload(args[1], args[2], args[3], activate)
		if e != nil {
			return e
		}
		data, err = httpClient.skillsRequest(ctx, "POST", path+"/versions", contentType, operationKey(), packageBody, 202, false)
		if err != nil {
			return err
		}
		return printSkillsData(data)
	default:
		return fmt.Errorf("unknown Skills command %q; use skills help", command)
	}
	if expected == 202 {
		operationKey()
	}
	data, err = httpClient.skillsJSON(ctx, method, path, key, body, expected)
	if err != nil {
		return err
	}
	return printSkillsData(data)
}

func printSkillsData(data []byte) error {
	var pretty bytes.Buffer
	if err := json.Indent(&pretty, data, "", "  "); err != nil {
		return err
	}
	fmt.Println(pretty.String())
	return nil
}
