//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

package service

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"time"

	"go.uber.org/zap"

	"multirag/internal/common"
	modelModule "multirag/internal/entity/models"
)

// MetaFilterCondition represents a single filter condition
type MetaFilterCondition struct {
	Key   string      `json:"key"`
	Value interface{} `json:"value"`
	Op    string      `json:"op"`
}

// MetaFilterResult represents the result of LLM-generated filter
type MetaFilterResult struct {
	Conditions []MetaFilterCondition `json:"conditions"`
	Logic      string                `json:"logic"`
}

// ManualValueResolver is a callback function to transform manual filter values
type ManualValueResolver func(map[string]interface{}) map[string]interface{}

// renderMetaFilterTemplate renders the Jinja2-like template from meta_filter.md
func renderMetaFilterTemplate(currentDate, metadataKeys, question, constraints string) (string, error) {
	templateContent, err := LoadPrompt("meta_filter")
	if err != nil {
		return "", err
	}

	// Replace variables
	result := strings.ReplaceAll(templateContent, "{{ current_date }}", currentDate)
	result = strings.ReplaceAll(result, "{{ metadata_keys }}", metadataKeys)
	result = strings.ReplaceAll(result, "{{ user_question }}", question)
	result = strings.ReplaceAll(result, "{{ constraints }}", constraints)

	// Handle {% if constraints %}...{% endif %}
	constraintRegex := regexp.MustCompile(`(?s)\{%\s*if\s+constraints\s*%\}(.+?)\{%\s*endif\s*%\}`)
	if constraints != "" {
		// Replace with the content inside the if block
		result = constraintRegex.ReplaceAllString(result, "$1")
	} else {
		// Remove the entire if block
		result = constraintRegex.ReplaceAllString(result, "")
	}

	// Clean up any extra newlines from removed blocks
	result = regexp.MustCompile(`\n{3,}`).ReplaceAllString(result, "\n\n")

	return strings.TrimSpace(result), nil
}

// GenMetaFilter generates filter conditions using LLM based on metadata and question.
func GenMetaFilter(ctx context.Context, chatModel *modelModule.ChatModel, metaData map[string]interface{}, question string, constraints map[string]string) (*MetaFilterResult, error) {
	if chatModel == nil {
		return nil, fmt.Errorf("chat model is nil")
	}

	if len(metaData) == 0 {
		return &MetaFilterResult{Conditions: []MetaFilterCondition{}, Logic: "and"}, nil
	}

	// Build metadata structure for prompt
	metaDataStructure := make(map[string][]string)
	for key, values := range metaData {
		if valueMap, ok := values.(map[string]interface{}); ok {
			keys := make([]string, 0, len(valueMap))
			for k := range valueMap {
				keys = append(keys, k)
			}
			metaDataStructure[key] = keys
		}
	}

	metaDataJSON, _ := json.Marshal(metaDataStructure)
	constraintsJSON := ""
	if constraints != nil {
		constraintsBytes, _ := json.Marshal(constraints)
		constraintsJSON = string(constraintsBytes)
	}

	// Build the prompt
	currentDate := time.Now().Format("2006-01-02")
	systemPrompt, err := renderMetaFilterTemplate(currentDate, string(metaDataJSON), question, constraintsJSON)
	if err != nil {
		return nil, fmt.Errorf("load metadata prompt: %w", err)
	}

	// Build user message
	userMessage := "Generate filters:"

	// Build messages: system prompt + user message
	messages := []modelModule.Message{
		{Role: "system", Content: systemPrompt},
		{Role: "user", Content: userMessage},
	}

	// Call the bound model with a request-local context.
	response, err := chatWithContext(ctx, chatModel, messages)
	if err != nil {
		common.Warn("Bound chat failed for GenMetaFilter",
			zap.String("provider", chatModel.ModelDriver.Name()),
			zap.String("model", *chatModel.ModelName),
			zap.Error(err))
		return nil, fmt.Errorf("failed to generate meta filter: %w", err)
	}

	// Clean up response
	if response == nil || response.Answer == nil {
		return nil, fmt.Errorf("metadata model returned no answer")
	}
	responseStr := strings.TrimSpace(*response.Answer)
	responseStr = thinkBlockRE.ReplaceAllString(responseStr, "")
	responseStr = strings.TrimSpace(responseStr)

	// Remove markdown code blocks if present
	responseStr = strings.TrimPrefix(responseStr, "```json")
	responseStr = strings.TrimPrefix(responseStr, "```")
	responseStr = strings.TrimSuffix(responseStr, "```")
	responseStr = strings.TrimSpace(responseStr)

	// Parse JSON
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal([]byte(responseStr), &envelope); err != nil || envelope == nil {
		return nil, fmt.Errorf("generated metadata filter must be an object")
	}
	conditions, present := envelope["conditions"]
	if !present || !strings.HasPrefix(strings.TrimSpace(string(conditions)), "[") {
		return nil, fmt.Errorf("generated metadata filter requires a conditions array")
	}
	if rawLogic, present := envelope["logic"]; present && !strings.HasPrefix(strings.TrimSpace(string(rawLogic)), `"`) {
		return nil, fmt.Errorf("generated metadata filter logic must be a string")
	}
	var result MetaFilterResult
	if err := json.Unmarshal([]byte(responseStr), &result); err != nil {
		return nil, fmt.Errorf("parse generated metadata filter: %w", err)
	}
	parsed := make([]common.MetaCondition, 0, len(result.Conditions))
	for _, condition := range result.Conditions {
		parsed = append(parsed, common.MetaCondition{Key: condition.Key, Operator: condition.Op, Value: condition.Value})
	}
	if !common.ValidMetaConditions(parsed, result.Logic) {
		return nil, fmt.Errorf("generated metadata filter contains an invalid expression")
	}

	common.Info("GenMetaFilter result", zap.Any("conditions", result.Conditions), zap.String("logic", result.Logic))

	return &result, nil
}

// ApplyMetaFilter adapts the existing flattened directory and delegates the
// entire expression to the common matcher, including boolean merging.
func ApplyMetaFilter(metaData map[string]interface{}, filters []MetaFilterCondition, logic string) []string {
	conditions := make([]common.MetaCondition, 0, len(filters))
	for _, filter := range filters {
		conditions = append(conditions, common.MetaCondition{Key: filter.Key, Operator: common.NormalizeOperator(filter.Op), Value: filter.Value})
	}
	return common.MetaFilter(metadataDirectory(metaData), &common.MetaFilterInput{Conditions: conditions, Logic: logic})
}

func metadataDirectory(metadata map[string]interface{}) common.MetaData {
	result := make(common.MetaData, len(metadata))
	for key, rawValues := range metadata {
		values := make(common.MetaValueDocs)
		switch raw := rawValues.(type) {
		case common.MetaValueDocs:
			result[key] = raw
			continue
		case map[string][]string:
			result[key] = common.MetaValueDocs(raw)
			continue
		case map[string]interface{}:
			for value, rawDocuments := range raw {
				switch documents := rawDocuments.(type) {
				case []string:
					values[value] = documents
				case []interface{}:
					for _, document := range documents {
						if documentID, ok := document.(string); ok {
							values[value] = append(values[value], documentID)
						}
					}
				}
			}
		}
		result[key] = values
	}
	return result
}

type metaFilterGenerator func(map[string]interface{}, string, map[string]string) (*MetaFilterResult, error)

// ValidateMetaDataFilter rejects malformed predicates before model or metadata
// access. Semi-auto field existence is checked against the loaded directory.
func ValidateMetaDataFilter(filter map[string]interface{}) error {
	if filter == nil {
		return nil
	}
	rawMethod, exists := filter["method"]
	if !exists {
		return nil
	}
	method, ok := rawMethod.(string)
	if !ok {
		return fmt.Errorf("metadata filter method must be a string")
	}
	switch method {
	case "auto":
		return nil
	case "manual":
		logic := "and"
		if raw, exists := filter["logic"]; exists {
			logic, ok = raw.(string)
			if !ok {
				return fmt.Errorf("metadata filter logic must be a string")
			}
		}
		var conditions []common.MetaCondition
		if raw, exists := filter["manual"]; exists {
			items, ok := raw.([]interface{})
			if !ok {
				return fmt.Errorf("manual metadata predicates must be a list")
			}
			for _, item := range items {
				condition, ok := item.(map[string]interface{})
				if !ok {
					return fmt.Errorf("manual metadata predicate must be an object")
				}
				key, keyOK := condition["key"].(string)
				operator, operatorOK := condition["op"].(string)
				if !keyOK || !operatorOK {
					return fmt.Errorf("manual metadata predicate requires key and operator")
				}
				conditions = append(conditions, common.MetaCondition{Key: key, Operator: operator, Value: condition["value"]})
			}
		}
		if !common.ValidMetaConditions(conditions, logic) {
			return fmt.Errorf("invalid manual metadata expression")
		}
		return nil
	case "semi_auto":
		items, ok := filter["semi_auto"].([]interface{})
		if !ok || len(items) == 0 {
			return fmt.Errorf("semi-auto metadata fields must be a nonempty list")
		}
		for _, item := range items {
			key, ok := item.(string)
			if condition, isCondition := item.(map[string]interface{}); isCondition {
				key, ok = condition["key"].(string)
				if rawOperator, exists := condition["op"]; exists {
					operator, valid := rawOperator.(string)
					if !valid || (operator != "" && !common.ValidMetaConditions([]common.MetaCondition{{Key: key, Operator: operator, Value: ""}}, "and")) {
						return fmt.Errorf("invalid semi-auto metadata operator")
					}
				}
			}
			if !ok || strings.TrimSpace(key) == "" {
				return fmt.Errorf("semi-auto metadata field requires a key")
			}
		}
		return nil
	default:
		// Existing configurations may carry an inactive/unknown method.
		return nil
	}
}

// ApplyMetaDataFilter narrows an optional base selection. nil means unrestricted;
// an explicit empty selection remains empty without invoking a model or resolver.
// No-match and invalid predicates use the existing -999 retrieval sentinel.
// The boolean reports an authoritative empty result, which callers must honor.
func ApplyMetaDataFilter(
	ctx context.Context,
	metaDataFilter map[string]interface{},
	metaData map[string]interface{},
	question string,
	chatModel *modelModule.ChatModel,
	baseDocIDs []string,
	manualValueResolver ...ManualValueResolver,
) ([]string, bool) {
	ids, empty, err := ApplyMetaDataFilterChecked(ctx, metaDataFilter, metaData, question, chatModel, baseDocIDs, manualValueResolver...)
	if err != nil {
		return []string{"-999"}, true
	}
	return ids, empty
}

// ApplyMetaDataFilterChecked distinguishes an empty match from an execution
// failure. Retrieval must propagate failures and cancellation to the caller.
func ApplyMetaDataFilterChecked(
	ctx context.Context,
	metaDataFilter map[string]interface{},
	metaData map[string]interface{},
	question string,
	chatModel *modelModule.ChatModel,
	baseDocIDs []string,
	manualValueResolver ...ManualValueResolver,
) ([]string, bool, error) {
	if err := ctx.Err(); err != nil {
		return nil, false, err
	}
	var generationError error
	generate := func(metadata map[string]interface{}, question string, constraints map[string]string) (*MetaFilterResult, error) {
		result, err := GenMetaFilter(ctx, chatModel, metadata, question, constraints)
		generationError = err
		return result, err
	}
	ids, empty := applyMetaDataFilter(ctx, metaDataFilter, metaData, question, baseDocIDs, manualValueResolver, generate)
	if err := ctx.Err(); err != nil {
		return nil, false, err
	}
	if generationError != nil {
		return nil, false, generationError
	}
	return ids, empty, nil
}

func applyMetaDataFilter(ctx context.Context, filter, metadata map[string]interface{}, question string, base []string, resolvers []ManualValueResolver, generate metaFilterGenerator) ([]string, bool) {
	if base != nil && len(base) == 0 {
		return []string{}, true
	}
	selected := cloneDocumentIDs(base)
	if filter == nil {
		return selected, false
	}
	if ctx.Err() != nil {
		return []string{"-999"}, true
	}
	if err := ValidateMetaDataFilter(filter); err != nil {
		return []string{"-999"}, true
	}
	method, _ := filter["method"].(string)
	var generated *MetaFilterResult
	var err error
	switch method {
	case "manual":
		conditions, exists := filter["manual"]
		if !exists {
			return selected, false
		}
		items, ok := conditions.([]interface{})
		if !ok {
			return []string{"-999"}, true
		}
		generated = &MetaFilterResult{Logic: "and"}
		if rawLogic, exists := filter["logic"]; exists {
			logic, ok := rawLogic.(string)
			if !ok {
				return []string{"-999"}, true
			}
			generated.Logic = logic
		}
		for _, raw := range items {
			condition, ok := raw.(map[string]interface{})
			if !ok {
				return []string{"-999"}, true
			}
			condition = cloneMetadataValue(condition).(map[string]interface{})
			if len(resolvers) > 0 && resolvers[0] != nil {
				condition = resolvers[0](condition)
			}
			key, keyOK := condition["key"].(string)
			operator, operatorOK := condition["op"].(string)
			if !keyOK || !operatorOK {
				return []string{"-999"}, true
			}
			generated.Conditions = append(generated.Conditions, MetaFilterCondition{Key: key, Op: operator, Value: condition["value"]})
		}
	case "auto":
		generated, err = generate(metadata, question, nil)
	case "semi_auto":
		selectedMetadata, constraints, ok := semiAutoMetadata(filter["semi_auto"], metadata)
		if !ok {
			return []string{"-999"}, true
		}
		generated, err = generate(selectedMetadata, question, constraints)
		if err == nil && generated != nil {
			if len(generated.Conditions) == 0 {
				return []string{"-999"}, true
			}
			for _, condition := range generated.Conditions {
				if _, exists := selectedMetadata[condition.Key]; !exists {
					return []string{"-999"}, true
				}
				if operator := constraints[condition.Key]; operator != "" && common.NormalizeOperator(condition.Op) != common.NormalizeOperator(operator) {
					return []string{"-999"}, true
				}
			}
		}
		metadata = selectedMetadata
	default:
		return selected, false
	}
	if err != nil || generated == nil || ctx.Err() != nil {
		return []string{"-999"}, true
	}
	conditions := make([]common.MetaCondition, 0, len(generated.Conditions))
	for _, condition := range generated.Conditions {
		conditions = append(conditions, common.MetaCondition{Key: condition.Key, Operator: condition.Op, Value: condition.Value})
	}
	if !common.ValidMetaConditions(conditions, generated.Logic) {
		return []string{"-999"}, true
	}
	if len(conditions) == 0 {
		return selected, false
	}
	matches := ApplyMetaFilter(metadata, generated.Conditions, generated.Logic)
	if base != nil {
		lookup := make(map[string]struct{}, len(matches))
		for _, document := range matches {
			lookup[document] = struct{}{}
		}
		matches = make([]string, 0, len(base))
		seen := make(map[string]struct{}, len(base))
		for _, document := range base {
			if _, exists := lookup[document]; !exists {
				continue
			}
			if _, exists := seen[document]; !exists {
				matches = append(matches, document)
				seen[document] = struct{}{}
			}
		}
	}
	if len(matches) == 0 {
		return []string{"-999"}, true
	}
	return matches, false
}

func cloneDocumentIDs(documents []string) []string {
	if documents == nil {
		return nil
	}
	return append([]string{}, documents...)
}

func cloneMetadataValue(value interface{}) interface{} {
	switch typed := value.(type) {
	case map[string]interface{}:
		copy := make(map[string]interface{}, len(typed))
		for key, item := range typed {
			copy[key] = cloneMetadataValue(item)
		}
		return copy
	case []interface{}:
		copy := make([]interface{}, len(typed))
		for index, item := range typed {
			copy[index] = cloneMetadataValue(item)
		}
		return copy
	case []string:
		return cloneDocumentIDs(typed)
	default:
		return value
	}
}

func semiAutoMetadata(selection interface{}, metadata map[string]interface{}) (map[string]interface{}, map[string]string, bool) {
	items, ok := selection.([]interface{})
	if !ok || len(items) == 0 {
		return nil, nil, false
	}
	selected := make(map[string]interface{}, len(items))
	constraints := make(map[string]string)
	directory := metadataDirectory(metadata)
	for _, item := range items {
		key, ok := item.(string)
		if condition, isCondition := item.(map[string]interface{}); isCondition {
			key, ok = condition["key"].(string)
			if rawOperator, exists := condition["op"]; exists {
				operator, valid := rawOperator.(string)
				if !valid {
					return nil, nil, false
				}
				if operator != "" {
					if !common.ValidMetaConditions([]common.MetaCondition{{Key: key, Operator: operator, Value: ""}}, "and") {
						return nil, nil, false
					}
					constraints[key] = common.NormalizeOperator(operator)
				}
			}
		}
		if !ok || strings.TrimSpace(key) == "" {
			return nil, nil, false
		}
		populated := false
		for _, documents := range directory[key] {
			populated = populated || len(documents) > 0
		}
		if !populated {
			return nil, nil, false
		}
		selected[key] = metadata[key]
	}
	return selected, constraints, true
}
