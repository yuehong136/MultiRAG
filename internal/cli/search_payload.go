package cli

import (
	"encoding/json"
	"fmt"
	"math"
	"strings"
)

// searchPayload validates every option before performing any dataset lookup.
// The same payload builder is used by ordinary and benchmark searches.
func (c *MultiRAGClient) searchPayload(cmd *Command) (map[string]interface{}, error) {
	if c.ServerType != "user" {
		return nil, fmt.Errorf("this command is only allowed in USER mode")
	}
	if cmd == nil {
		return nil, fmt.Errorf("search command required")
	}
	question, ok := cmd.Params["question"].(string)
	if !ok || strings.TrimSpace(question) == "" {
		return nil, fmt.Errorf("a non-empty search question is required")
	}
	options := make(map[string]interface{})
	if raw, exists := cmd.Params["options"]; exists {
		options, ok = raw.(map[string]interface{})
		if !ok || options == nil {
			return nil, fmt.Errorf("search options must be an object")
		}
	}
	payload, err := searchOptionsPayload(options)
	if err != nil {
		return nil, err
	}
	payload["question"] = question
	if raw, exists := cmd.Params["dataset_ids"]; exists {
		ids, ok := raw.([]string)
		if !ok || len(ids) == 0 {
			return nil, fmt.Errorf("resolved dataset_ids must be a non-empty string list")
		}
		if err := validateSearchStrings("dataset_ids", ids); err != nil {
			return nil, err
		}
		payload["dataset_ids"] = append([]string{}, ids...)
		return payload, nil
	}
	datasets, ok := cmd.Params["datasets"].(string)
	if !ok {
		return nil, fmt.Errorf("datasets must be a quoted dataset name list")
	}
	names, err := parseSearchDatasetNames(datasets)
	if err != nil {
		return nil, err
	}
	ids := make([]string, 0, len(names))
	seen := make(map[string]bool, len(names))
	for _, name := range names {
		id, err := c.getDatasetID(name)
		if err != nil {
			return nil, err
		}
		if strings.TrimSpace(id) == "" || seen[id] {
			return nil, fmt.Errorf("dataset names must resolve to distinct non-empty IDs")
		}
		seen[id] = true
		ids = append(ids, id)
	}
	payload["dataset_ids"] = ids
	return payload, nil
}

func searchOptionName(value string) (string, error) {
	name := strings.ToLower(value)
	if name == "page_size" {
		name = "size"
	}
	switch name {
	case "top_k", "page", "size", "similarity_threshold", "vector_similarity_weight",
		"keyword", "use_kg", "rerank_id", "tenant_rerank_id", "search_id",
		"meta_data_filter", "cross_languages", "doc_ids":
		return name, nil
	default:
		return "", fmt.Errorf("unknown search option %q", value)
	}
}

func searchOptionsPayload(options map[string]interface{}) (map[string]interface{}, error) {
	payload := map[string]interface{}{
		"similarity_threshold":     0.2,
		"vector_similarity_weight": 0.3,
	}
	seen := make(map[string]bool, len(options))
	for rawName, value := range options {
		name, err := searchOptionName(rawName)
		if err != nil {
			return nil, err
		}
		if seen[name] {
			return nil, fmt.Errorf("duplicate search option %q", name)
		}
		seen[name] = true
		switch name {
		case "top_k", "page", "size":
			number, ok := value.(int)
			minimum := 1
			if name == "top_k" {
				minimum = 0
			}
			if !ok || number < minimum {
				return nil, fmt.Errorf("search option %q must be an integer >= %d", name, minimum)
			}
			payload[name] = number
		case "similarity_threshold", "vector_similarity_weight":
			var number float64
			switch v := value.(type) {
			case int:
				number = float64(v)
			case float64:
				number = v
			default:
				return nil, fmt.Errorf("search option %q must be a number", name)
			}
			if math.IsNaN(number) || math.IsInf(number, 0) || number < 0 || number > 1 {
				return nil, fmt.Errorf("search option %q must be finite and between 0 and 1", name)
			}
			payload[name] = number
		case "keyword", "use_kg":
			boolean, ok := value.(bool)
			if !ok {
				return nil, fmt.Errorf("search option %q must be a boolean", name)
			}
			payload[name] = boolean
		case "cross_languages", "doc_ids":
			values, ok := value.([]string)
			if !ok || values == nil {
				return nil, fmt.Errorf("search option %q must be a string list", name)
			}
			if err := validateSearchStrings(name, values); err != nil {
				return nil, err
			}
			payload[name] = append([]string{}, values...)
		case "meta_data_filter":
			raw, ok := value.(string)
			if !ok {
				return nil, fmt.Errorf("meta_data_filter must be a quoted JSON object")
			}
			var filter map[string]interface{}
			if err := json.Unmarshal([]byte(raw), &filter); err != nil || filter == nil {
				return nil, fmt.Errorf("meta_data_filter must be a valid JSON object")
			}
			payload[name] = filter
		case "tenant_rerank_id":
			var id int64
			switch number := value.(type) {
			case int64:
				id = number
			case int:
				id = int64(number)
			default:
				return nil, fmt.Errorf("tenant_rerank_id must be a positive integer model ID")
			}
			if id <= 0 {
				return nil, fmt.Errorf("tenant_rerank_id must be a positive integer model ID")
			}
			payload[name] = id
		default:
			selector, ok := value.(string)
			if !ok || strings.TrimSpace(selector) == "" {
				return nil, fmt.Errorf("search option %q must be a non-empty quoted string", name)
			}
			payload[name] = selector
		}
	}
	if seen["rerank_id"] && seen["tenant_rerank_id"] {
		return nil, fmt.Errorf("rerank_id and tenant_rerank_id cannot be combined")
	}
	return payload, nil
}

func parseSearchDatasetNames(raw string) ([]string, error) {
	parts := strings.Split(raw, ",")
	names := make([]string, len(parts))
	for i, part := range parts {
		names[i] = strings.TrimSpace(part)
	}
	if err := validateSearchStrings("datasets", names); err != nil {
		return nil, err
	}
	return names, nil
}

func validateSearchStrings(name string, values []string) error {
	seen := make(map[string]bool, len(values))
	for _, value := range values {
		if strings.TrimSpace(value) == "" || seen[value] {
			return fmt.Errorf("%s must contain distinct non-empty strings", name)
		}
		seen[value] = true
	}
	return nil
}
