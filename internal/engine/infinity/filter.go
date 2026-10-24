package infinity

import (
	"fmt"
	"multirag/internal/engine/types"
	"sort"
	"strings"
)

// strictFilterFromCondition never broadens an unrepresentable condition. The
// schema is required for mutations; nil is used by search where the server
// validates columns and unknown columns fail instead of being discarded.
func strictFilterFromCondition(condition map[string]interface{}, columns map[string]struct {
	Type    string
	Default interface{}
}) (string, error) {
	keys := make([]string, 0, len(condition))
	for k := range condition {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, key := range keys {
		value := condition[key]
		if key == "exists" || key == "must_not" {
			if key == "must_not" {
				m, ok := value.(map[string]interface{})
				if !ok || len(m) != 1 || m["exists"] == nil {
					return "", fmt.Errorf("unsupported must_not condition")
				}
				value = m["exists"]
			}
			field, ok := value.(string)
			if !ok || !types.ValidFieldName(field) {
				return "", fmt.Errorf("invalid exists field")
			}
			if columns != nil {
				if _, ok := columns[field]; !ok {
					return "", fmt.Errorf("unknown condition column %q", field)
				}
			}
			part := existsCondition(field, columns)
			if key == "must_not" {
				part = "NOT (" + part + ")"
			}
			parts = append(parts, part)
			continue
		}
		if !types.ValidFieldName(key) {
			return "", fmt.Errorf("invalid condition field %q", key)
		}
		if columns != nil {
			if _, ok := columns[key]; !ok {
				return "", fmt.Errorf("unknown condition column %q", key)
			}
		}
		values, list, err := types.ConditionValues(value)
		if err != nil {
			return "", fmt.Errorf("condition %s: %w", key, err)
		}
		literals := make([]string, 0, len(values))
		for _, v := range values {
			if str, ok := v.(string); ok {
				literal, err := filterLiteral(str)
				if err != nil {
					return "", err
				}
				literals = append(literals, literal)
			} else {
				literals = append(literals, fmt.Sprint(v))
			}
		}
		if fieldKeyword(key) {
			ors := make([]string, 0, len(values))
			for _, v := range values {
				str, ok := v.(string)
				if !ok {
					return "", fmt.Errorf("keyword filter requires strings")
				}
				literal, err := filterLiteral(str)
				if err != nil {
					return "", err
				}
				ors = append(ors, fmt.Sprintf("filter_fulltext('%s', %s)", convertMatchingField(key), literal))
			}
			parts = append(parts, "("+strings.Join(ors, " OR ")+")")
			continue
		}
		if list {
			parts = append(parts, key+" IN ("+strings.Join(literals, ", ")+")")
		} else {
			parts = append(parts, key+" = "+literals[0])
		}
	}
	if len(parts) == 0 {
		return "1=1", nil
	}
	return strings.Join(parts, " AND "), nil
}

// The pinned Go SDK sends string literals as Thrift constants and does not
// unescape SQL doubled quotes. Pick a delimiter absent from the value; refuse
// values it cannot represent exactly rather than silently targeting zero rows.
func filterLiteral(value string) (string, error) {
	if !strings.Contains(value, "'") {
		return "'" + value + "'", nil
	}
	if !strings.Contains(value, "\"") {
		return "\"" + value + "\"", nil
	}
	return "", fmt.Errorf("the pinned Infinity SDK cannot represent a filter value containing both quote delimiters")
}
