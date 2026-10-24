package models

import (
	"encoding/json"
	"fmt"
)

// Azure sends prompt and completion annotations separately from text deltas.
// A filtering signal must survive even when it arrives after finish_reason=stop.
func azureOpenAIStreamFrame(raw []byte) (bool, error) {
	var frame struct {
		Error               json.RawMessage `json:"error"`
		PromptFilterResults json.RawMessage `json:"prompt_filter_results"`
		PromptAnnotations   json.RawMessage `json:"prompt_annotations"`
		Choices             *[]*struct {
			Index   *int            `json:"index"`
			Finish  *string         `json:"finish_reason"`
			Delta   json.RawMessage `json:"delta"`
			Results json.RawMessage `json:"content_filter_results"`
			Offsets json.RawMessage `json:"content_filter_offsets"`
		} `json:"choices"`
	}
	if err := json.Unmarshal(raw, &frame); err != nil {
		return false, fmt.Errorf("invalid Azure OpenAI stream frame")
	}
	if len(frame.Error) > 0 && string(frame.Error) != "null" {
		return false, fmt.Errorf("Azure OpenAI stream provider error")
	}
	prompt := false
	for _, annotation := range []json.RawMessage{frame.PromptFilterResults, frame.PromptAnnotations} {
		if len(annotation) > 0 {
			if err := azureOpenAIPromptAnnotations(annotation); err != nil {
				return false, err
			}
			prompt = true
		}
	}
	if frame.Choices == nil {
		if prompt {
			return false, fmt.Errorf("missing Azure OpenAI annotation choices")
		}
		return false, nil
	}
	if len(*frame.Choices) == 0 {
		return prompt, nil
	}
	choice := (*frame.Choices)[0]
	if choice == nil {
		return false, nil
	}
	missingDelta := len(choice.Delta) == 0 || string(choice.Delta) == "null"
	if err := azureOpenAIFilterResult(choice.Finish, choice.Results, missingDelta); err != nil {
		return false, err
	}
	if len(choice.Offsets) > 0 {
		if err := azureOpenAIContentOffsets(choice.Offsets); err != nil {
			return false, err
		}
	}
	if missingDelta {
		if len(choice.Delta) > 0 || choice.Index == nil || *choice.Index < 0 || len(choice.Offsets) == 0 {
			return false, fmt.Errorf("invalid Azure OpenAI content annotation")
		}
		return true, nil
	}
	return false, nil
}

func azureOpenAIFilterResult(finish *string, raw json.RawMessage, required bool) error {
	if finish != nil && *finish == "content_filter" {
		return fmt.Errorf("Azure OpenAI content was filtered")
	}
	if len(raw) == 0 || string(raw) == "null" {
		if required {
			return fmt.Errorf("missing Azure OpenAI content annotation")
		}
		return nil
	}
	var categories map[string]json.RawMessage
	if err := json.Unmarshal(raw, &categories); err != nil || categories == nil {
		return fmt.Errorf("invalid Azure OpenAI content annotation")
	}
	for name, value := range categories {
		if name == "error" {
			return fmt.Errorf("Azure OpenAI content filter failed")
		}
		var fields map[string]json.RawMessage
		if err := json.Unmarshal(value, &fields); err != nil || fields == nil {
			return fmt.Errorf("invalid Azure OpenAI filter category")
		}
		for _, key := range []string{"filtered", "detected"} {
			if raw, ok := fields[key]; ok {
				var flag *bool
				if err := json.Unmarshal(raw, &flag); err != nil || flag == nil {
					return fmt.Errorf("invalid Azure OpenAI filter flag")
				}
				if key == "filtered" && *flag {
					return fmt.Errorf("Azure OpenAI content was filtered")
				}
			}
		}
	}
	return nil
}

func azureOpenAIPromptAnnotations(raw json.RawMessage) error {
	var annotations *[]struct {
		Index   *int            `json:"prompt_index"`
		Results json.RawMessage `json:"content_filter_results"`
	}
	if err := json.Unmarshal(raw, &annotations); err != nil || annotations == nil {
		return fmt.Errorf("invalid Azure OpenAI prompt annotations")
	}
	seen := map[int]bool{}
	for _, annotation := range *annotations {
		if annotation.Index == nil || *annotation.Index < 0 || seen[*annotation.Index] {
			return fmt.Errorf("invalid Azure OpenAI prompt index")
		}
		seen[*annotation.Index] = true
		if err := azureOpenAIFilterResult(nil, annotation.Results, true); err != nil {
			return err
		}
	}
	return nil
}

func azureOpenAIContentOffsets(raw json.RawMessage) error {
	var offsets struct {
		Check *int `json:"check_offset"`
		Start *int `json:"start_offset"`
		End   *int `json:"end_offset"`
	}
	if err := json.Unmarshal(raw, &offsets); err != nil || offsets.Check == nil || offsets.Start == nil || offsets.End == nil || *offsets.Check < 0 || *offsets.Start < 0 || *offsets.End < *offsets.Start {
		return fmt.Errorf("invalid Azure OpenAI annotation offsets")
	}
	return nil
}
