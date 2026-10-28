package common

import (
	"encoding/json"
	"math"
	"math/big"
	"sort"
	"strconv"
	"strings"
)

// MetaCondition is a predicate over one metadata field.
type MetaCondition struct {
	Operator string
	Key      string
	Value    interface{}
}

// MetaValueDocs maps each flattened field value to its documents.
type MetaValueDocs map[string][]string

// MetaData is the flattened metadata directory used during retrieval.
type MetaData map[string]MetaValueDocs

// MetaFilterInput groups predicates with their boolean operator.
type MetaFilterInput struct {
	Conditions []MetaCondition
	Logic      string
	invalid    bool
}

// NormalizeOperator translates API aliases to the shared predicate symbols.
func NormalizeOperator(operator string) string {
	switch operator {
	case "is", "==":
		return "="
	case "not is", "!=":
		return "≠"
	case ">=":
		return "≥"
	case "<=":
		return "≤"
	default:
		return operator
	}
}

// ParseAndConvert preserves invalid predicates so a malformed filter cannot
// become an absent filter. Only an absent or explicitly empty list means no filter.
func ParseAndConvert(condition map[string]interface{}) *MetaFilterInput {
	if condition == nil {
		return nil
	}
	input := &MetaFilterInput{Logic: "and"}
	if raw, exists := condition["logic"]; exists {
		var ok bool
		input.Logic, ok = raw.(string)
		input.invalid = !ok
	}
	input.invalid = input.invalid || !ValidMetaConditions(nil, input.Logic)
	raw, exists := condition["conditions"]
	if !exists {
		if input.invalid {
			return input
		}
		return nil
	}
	conditions, ok := raw.([]interface{})
	if !ok {
		input.invalid = true
		return input
	}
	if len(conditions) == 0 && !input.invalid {
		return nil
	}
	for _, rawCondition := range conditions {
		item, ok := rawCondition.(map[string]interface{})
		if !ok {
			input.invalid = true
			continue
		}
		key, keyOK := item["name"].(string)
		operator, operatorOK := item["comparison_operator"].(string)
		input.invalid = input.invalid || !keyOK || !operatorOK
		input.Conditions = append(input.Conditions, MetaCondition{Key: key, Operator: NormalizeOperator(operator), Value: item["value"]})
	}
	return input
}

// ValidMetaConditions validates the entire expression before evaluating AND/OR.
// A malformed exclusion must invalidate an OR expression as well.
func ValidMetaConditions(conditions []MetaCondition, logic string) bool {
	if logic != "" && logic != "and" && logic != "or" {
		return false
	}
	for _, condition := range conditions {
		if strings.TrimSpace(condition.Key) == "" {
			return false
		}
		switch NormalizeOperator(condition.Operator) {
		case "empty", "not empty":
			continue
		case "in", "not in":
			if _, ok := metadataMembership(condition.Value); !ok {
				return false
			}
		case "=", "≠", ">", "<", "≥", "≤", "contains", "not contains", "start with", "end with":
			if _, ok := metadataScalar(condition.Value); !ok {
				return false
			}
		default:
			return false
		}
	}
	return true
}

// MetaFilter is the single matcher for API and model-generated predicates.
// Missing fields never match, including negative and empty predicates.
func MetaFilter(metadata MetaData, input *MetaFilterInput) []string {
	if input == nil {
		return nil
	}
	if input.invalid || !ValidMetaConditions(input.Conditions, input.Logic) {
		return []string{}
	}
	if len(input.Conditions) == 0 {
		return nil
	}
	var matches map[string]struct{}
	for _, condition := range input.Conditions {
		current := make(map[string]struct{})
		operator := NormalizeOperator(condition.Operator)
		var lookup map[string]struct{}
		if operator == "in" || operator == "not in" {
			values, _ := metadataMembership(condition.Value)
			lookup = make(map[string]struct{}, len(values))
			for _, value := range values {
				if operator == "not in" {
					value = strings.ToLower(value)
				}
				lookup[value] = struct{}{}
			}
		}
		for value, documents := range metadata[condition.Key] {
			matched := false
			if lookup != nil {
				if operator == "not in" {
					value = strings.ToLower(value)
				}
				_, matched = lookup[value]
				if operator == "not in" {
					matched = !matched
				}
			} else {
				matched = matchMetadataValue(value, operator, condition.Value)
			}
			if matched {
				for _, document := range documents {
					current[document] = struct{}{}
				}
			}
		}
		if matches == nil {
			matches = current
		} else if input.Logic == "or" {
			for document := range current {
				matches[document] = struct{}{}
			}
		} else {
			for document := range matches {
				if _, exists := current[document]; !exists {
					delete(matches, document)
				}
			}
		}
		if input.Logic != "or" && len(matches) == 0 {
			return []string{}
		}
	}
	result := make([]string, 0, len(matches))
	for document := range matches {
		result = append(result, document)
	}
	sort.Strings(result)
	return result
}

func metadataMembership(value interface{}) ([]string, bool) {
	var values []interface{}
	switch items := value.(type) {
	case string:
		// Existing Go manual filters use comma-separated membership operands.
		for _, item := range strings.Split(items, ",") {
			if item = strings.TrimSpace(item); item != "" {
				values = append(values, item)
			}
		}
	case []interface{}:
		values = items
	case []string:
		for _, item := range items {
			values = append(values, item)
		}
	default:
		return nil, false
	}
	result := make([]string, 0, len(values))
	for _, item := range values {
		text, ok := metadataScalar(item)
		if !ok {
			return nil, false
		}
		result = append(result, text)
	}
	return result, true
}

func metadataScalar(value interface{}) (string, bool) {
	switch scalar := value.(type) {
	case string:
		return scalar, true
	case bool:
		return strconv.FormatBool(scalar), true
	case int:
		return strconv.Itoa(scalar), true
	case int8:
		return strconv.FormatInt(int64(scalar), 10), true
	case int16:
		return strconv.FormatInt(int64(scalar), 10), true
	case int32:
		return strconv.FormatInt(int64(scalar), 10), true
	case int64:
		return strconv.FormatInt(scalar, 10), true
	case uint:
		return strconv.FormatUint(uint64(scalar), 10), true
	case uint8:
		return strconv.FormatUint(uint64(scalar), 10), true
	case uint16:
		return strconv.FormatUint(uint64(scalar), 10), true
	case uint32:
		return strconv.FormatUint(uint64(scalar), 10), true
	case uint64:
		return strconv.FormatUint(scalar, 10), true
	case float32:
		return metadataFloat(float64(scalar), 32)
	case float64:
		return metadataFloat(scalar, 64)
	case json.Number:
		if number, err := scalar.Float64(); err == nil && !math.IsNaN(number) && !math.IsInf(number, 0) {
			return scalar.String(), true
		}
	}
	return "", false
}

func metadataFloat(value float64, bits int) (string, bool) {
	if math.IsNaN(value) || math.IsInf(value, 0) {
		return "", false
	}
	return strconv.FormatFloat(value, 'f', -1, bits), true
}

func matchMetadataValue(value, operator string, operand interface{}) bool {
	if operator == "empty" {
		return value == ""
	}
	if operator == "not empty" {
		return value != ""
	}
	filter, _ := metadataScalar(operand)
	valueLower, filterLower := strings.ToLower(value), strings.ToLower(filter)
	switch operator {
	case "contains":
		return strings.Contains(valueLower, filterLower)
	case "not contains":
		return !strings.Contains(valueLower, filterLower)
	case "start with":
		return strings.HasPrefix(valueLower, filterLower)
	case "end with":
		return strings.HasSuffix(valueLower, filterLower)
	}
	if metadataDate(filter) && !metadataDate(value) {
		return operator == "≠"
	}
	left, leftErr := strconv.ParseFloat(value, 64)
	right, rightErr := strconv.ParseFloat(filter, 64)
	comparison := strings.Compare(valueLower, filterLower)
	if leftErr == nil && rightErr == nil && !math.IsNaN(left) && !math.IsNaN(right) && !math.IsInf(left, 0) && !math.IsInf(right, 0) {
		// Keep large integers and decimal values exact after recognizing a
		// numeric spelling; float64 equality would conflate adjacent integers.
		leftNumber, leftOK := new(big.Rat).SetString(value)
		rightNumber, rightOK := new(big.Rat).SetString(filter)
		if leftOK && rightOK {
			comparison = leftNumber.Cmp(rightNumber)
		}
	}
	switch operator {
	case "=":
		return comparison == 0
	case "≠":
		return comparison != 0
	case ">":
		return comparison > 0
	case "<":
		return comparison < 0
	case "≥":
		return comparison >= 0
	case "≤":
		return comparison <= 0
	}
	return false
}

func metadataDate(value string) bool {
	if len(value) != 10 || value[4] != '-' || value[7] != '-' {
		return false
	}
	for index, character := range value {
		if index != 4 && index != 7 && (character < '0' || character > '9') {
			return false
		}
	}
	return true
}
