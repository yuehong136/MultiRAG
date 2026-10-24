package types

import (
	"encoding/json"
	"fmt"
	"math"
	"reflect"
	"regexp"
	"strings"
)

var fieldName = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

func ValidFieldName(field string) bool { return fieldName.MatchString(field) }

// ConditionValues accepts both JSON arrays and Go typed slices without silently
// dropping a condition. Empty, malformed and non-finite values are rejected.
func ConditionValues(value interface{}) ([]interface{}, bool, error) {
	if value == nil {
		return nil, false, fmt.Errorf("condition value is null")
	}
	rv := reflect.ValueOf(value)
	list := rv.Kind() == reflect.Slice || rv.Kind() == reflect.Array
	if list && rv.Type().Elem().Kind() == reflect.Uint8 {
		return nil, false, fmt.Errorf("binary condition is unsupported")
	}
	values := []interface{}{value}
	if list {
		values = make([]interface{}, rv.Len())
		for i := range values {
			values[i] = rv.Index(i).Interface()
		}
	}
	if len(values) == 0 {
		return nil, list, fmt.Errorf("condition list is empty")
	}
	for _, v := range values {
		switch n := v.(type) {
		case string:
			if strings.TrimSpace(n) == "" {
				return nil, list, fmt.Errorf("condition string is empty")
			}
		case bool, int, int8, int16, int32, int64, uint, uint8, uint16, uint32, uint64:
		case float32:
			if math.IsNaN(float64(n)) || math.IsInf(float64(n), 0) {
				return nil, list, fmt.Errorf("condition number is not finite")
			}
		case float64:
			if math.IsNaN(n) || math.IsInf(n, 0) {
				return nil, list, fmt.Errorf("condition number is not finite")
			}
		case json.Number:
			f, err := n.Float64()
			if err != nil || math.IsNaN(f) || math.IsInf(f, 0) {
				return nil, list, fmt.Errorf("invalid condition number")
			}
		default:
			return nil, list, fmt.Errorf("unsupported condition value %T", v)
		}
	}
	return values, list, nil
}
