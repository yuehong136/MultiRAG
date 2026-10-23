package models

import (
	"encoding/json"
	"fmt"
	"strings"
)

// FileParser submits an asynchronous parse job. A pending job is never a parsed document.
type FileParser interface {
	ParseFile(*string, []byte, string, *APIConfig) (*ParseTask, error)
}

type TaskReader interface {
	ListTasks(*APIConfig) ([]ParseTask, error)
	ShowTask(string, *APIConfig) (*ParseTask, error)
}

type TaskSegment struct {
	Index   int    `json:"index"`
	Content string `json:"content"`
}

// UnmarshalJSON distinguishes a legal zero index from an omitted or null field.
func (s *TaskSegment) UnmarshalJSON(raw []byte) error {
	var fields struct {
		Index   *int    `json:"index"`
		Content *string `json:"content"`
	}
	if err := json.Unmarshal(raw, &fields); err != nil {
		return fmt.Errorf("invalid parse segment")
	}
	if fields.Index == nil || fields.Content == nil {
		return fmt.Errorf("parse segment requires index and content")
	}
	s.Index, s.Content = *fields.Index, *fields.Content
	return nil
}

type ParseTask struct {
	TaskID   string        `json:"task_id"`
	Status   string        `json:"status"`
	Segments []TaskSegment `json:"segments,omitempty"`
}

func ValidateTaskID(id string) error {
	if id == "" || len(id) > 256 {
		return fmt.Errorf("invalid task ID")
	}
	for _, ch := range id {
		if !(ch >= 'a' && ch <= 'z' || ch >= 'A' && ch <= 'Z' || ch >= '0' && ch <= '9' || ch == '-' || ch == '_') {
			return fmt.Errorf("invalid task ID")
		}
	}
	return nil
}

func ValidateParseTask(task *ParseTask, result bool) error {
	if task == nil {
		return fmt.Errorf("missing parse task")
	}
	if err := ValidateTaskID(task.TaskID); err != nil {
		return err
	}
	switch task.Status {
	case "pending", "queued", "running", "processing", "success", "failed", "cancelled":
	default:
		return fmt.Errorf("unknown parse task status")
	}
	if !result {
		return nil
	}
	if task.Status == "failed" || task.Status == "cancelled" {
		return fmt.Errorf("parse task %s", task.Status)
	}
	if task.Status != "success" {
		if len(task.Segments) != 0 {
			return fmt.Errorf("unfinished parse task has results")
		}
		return nil
	}
	if len(task.Segments) == 0 {
		return fmt.Errorf("successful parse task has no segments")
	}
	seen, size := map[int]bool{}, 0
	for _, segment := range task.Segments {
		size += len(segment.Content)
		if segment.Index < 0 || seen[segment.Index] || strings.TrimSpace(segment.Content) == "" || size > MaxMediaBytes {
			return fmt.Errorf("invalid parse segments")
		}
		seen[segment.Index] = true
	}
	return nil
}

func ValidateParseInput(content []byte, documentURL string) error {
	if (len(content) > 0) == (documentURL != "") {
		return fmt.Errorf("provide file content or URL exclusively")
	}
	if documentURL != "" {
		return ValidateOCRURL(documentURL)
	}
	return ValidateMediaBytes(content)
}
