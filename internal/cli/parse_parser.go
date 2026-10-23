package cli

import (
	"fmt"
	"multirag/internal/entity/models"
	"strings"
)

func (p *Parser) finishParseCommand() error {
	if p.curToken.Type != TokenSemicolon {
		return fmt.Errorf("expected semicolon")
	}
	p.nextToken()
	if p.curToken.Type != TokenEOF {
		return fmt.Errorf("unexpected trailing input")
	}
	return nil
}

func (p *Parser) parseFileCommand() (*Command, error) {
	cmd := NewCommand("parse_file_user_command")
	if p.curToken.Type == TokenWith {
		p.nextToken()
		name, err := p.parseQuotedString()
		if err != nil {
			return nil, err
		}
		parts := strings.Split(name, "@")
		if len(parts) != 3 {
			return nil, fmt.Errorf("model must be model@instance@provider")
		}
		for _, part := range parts {
			if strings.TrimSpace(part) == "" {
				return nil, fmt.Errorf("empty model identifier")
			}
		}
		cmd.Params["composite_model_name"] = name
		p.nextToken()
	}
	key := "file"
	if p.curToken.Type == TokenURL {
		key = "url"
	} else if p.curToken.Type != TokenFile {
		return nil, fmt.Errorf("expected FILE or URL")
	}
	p.nextToken()
	value, err := p.parseQuotedString()
	if err != nil || strings.TrimSpace(value) == "" {
		return nil, fmt.Errorf("file input required")
	}
	if key == "url" {
		if err := models.ValidateOCRURL(value); err != nil {
			return nil, err
		}
	}
	cmd.Params[key] = value
	p.nextToken()
	if err := p.finishParseCommand(); err != nil {
		return nil, err
	}
	return cmd, nil
}

func (p *Parser) parseProviderTask(show bool) (*Command, error) {
	name, err := p.parseQuotedString()
	if err != nil {
		return nil, err
	}
	parts := strings.Split(name, "@")
	if len(parts) != 2 || strings.TrimSpace(parts[0]) == "" || strings.TrimSpace(parts[1]) == "" {
		return nil, fmt.Errorf("instance must be instance@provider")
	}
	p.nextToken()
	kind := "list_tasks_user_command"
	cmd := NewCommand(kind)
	if show {
		if p.curToken.Type != TokenTask {
			return nil, fmt.Errorf("expected TASK")
		}
		p.nextToken()
		id, err := p.parseQuotedString()
		if err != nil {
			return nil, err
		}
		if err := models.ValidateTaskID(id); err != nil {
			return nil, err
		}
		cmd.Type = "show_task_user_command"
		cmd.Params["task_id"] = id
		p.nextToken()
	} else {
		if p.curToken.Type != TokenTasks {
			return nil, fmt.Errorf("expected TASKS")
		}
		p.nextToken()
	}
	cmd.Params["provider_name"], cmd.Params["instance_name"] = parts[1], parts[0]
	if err := p.finishParseCommand(); err != nil {
		return nil, err
	}
	return cmd, nil
}
