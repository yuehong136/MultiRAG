package common

import (
	"math"
	"reflect"
	"testing"
)

func TestMetaFilterOperators(t *testing.T) {
	metadata := MetaData{
		"name":    {"Alice Report": {"alice"}, "bob": {"bob"}, "": {"empty"}},
		"score":   {"0": {"zero"}, "2": {"two"}, "10": {"ten"}},
		"enabled": {"false": {"disabled"}, "true": {"enabled"}},
		"date":    {"2024-06-01": {"june"}, "2024-07-01": {"july"}, "unknown": {"unknown"}},
		"large":   {"9007199254740992": {"lower"}, "9007199254740993": {"higher"}},
	}
	cases := []struct {
		name      string
		condition MetaCondition
		want      []string
	}{
		{"equality alias", MetaCondition{Key: "name", Operator: "is", Value: "ALICE REPORT"}, []string{"alice"}},
		{"double equality", MetaCondition{Key: "score", Operator: "==", Value: 0}, []string{"zero"}},
		{"inequality alias", MetaCondition{Key: "score", Operator: "not is", Value: 0}, []string{"ten", "two"}},
		{"greater numeric", MetaCondition{Key: "score", Operator: ">", Value: 2}, []string{"ten"}},
		{"less numeric", MetaCondition{Key: "score", Operator: "<", Value: 2}, []string{"zero"}},
		{"greater equal", MetaCondition{Key: "score", Operator: ">=", Value: float64(2)}, []string{"ten", "two"}},
		{"less equal", MetaCondition{Key: "score", Operator: "<=", Value: 2}, []string{"two", "zero"}},
		{"contains case folded", MetaCondition{Key: "name", Operator: "contains", Value: "REPORT"}, []string{"alice"}},
		{"not contains case folded", MetaCondition{Key: "name", Operator: "not contains", Value: "REPORT"}, []string{"bob", "empty"}},
		{"start case folded", MetaCondition{Key: "name", Operator: "start with", Value: "AL"}, []string{"alice"}},
		{"end case folded", MetaCondition{Key: "name", Operator: "end with", Value: "PORT"}, []string{"alice"}},
		{"empty", MetaCondition{Key: "name", Operator: "empty"}, []string{"empty"}},
		{"not empty", MetaCondition{Key: "name", Operator: "not empty"}, []string{"alice", "bob"}},
		{"false is a value", MetaCondition{Key: "enabled", Operator: "=", Value: false}, []string{"disabled"}},
		{"large integers remain distinct", MetaCondition{Key: "large", Operator: "=", Value: int64(9007199254740993)}, []string{"higher"}},
		{"numeric membership", MetaCondition{Key: "score", Operator: "in", Value: []interface{}{0, 2.0}}, []string{"two", "zero"}},
		{"boolean membership", MetaCondition{Key: "enabled", Operator: "in", Value: []interface{}{false}}, []string{"disabled"}},
		{"in retains exact text contract", MetaCondition{Key: "name", Operator: "in", Value: []string{"BOB"}}, []string{}},
		{"not in case folded", MetaCondition{Key: "name", Operator: "not in", Value: []string{"BOB", ""}}, []string{"alice"}},
		{"legacy CSV membership", MetaCondition{Key: "score", Operator: "in", Value: " 0, 2 "}, []string{"two", "zero"}},
		{"empty membership", MetaCondition{Key: "score", Operator: "in", Value: []interface{}{}}, []string{}},
		{"empty exclusion", MetaCondition{Key: "score", Operator: "not in", Value: []interface{}{}}, []string{"ten", "two", "zero"}},
		{"date comparison", MetaCondition{Key: "date", Operator: ">", Value: "2024-06-01"}, []string{"july"}},
		{"non date inequality", MetaCondition{Key: "date", Operator: "!=", Value: "2024-06-01"}, []string{"july", "unknown"}},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			got := MetaFilter(metadata, &MetaFilterInput{Conditions: []MetaCondition{test.condition}})
			if !reflect.DeepEqual(got, test.want) {
				t.Fatalf("MetaFilter() = %v, want %v", got, test.want)
			}
		})
	}
}

func TestMetaFilterBooleanScope(t *testing.T) {
	metadata := MetaData{"status": {"ready": {"first", "second"}}, "version": {"v2": {"second", "third"}}}
	for _, logic := range []string{"and", "or"} {
		for _, operator := range []string{"is", "not is", "empty", "not empty", "not contains", "not in"} {
			conditions := []MetaCondition{{Key: "missing", Operator: operator, Value: "x"}, {Key: "status", Operator: "is", Value: "ready"}}
			want := []string{}
			if logic == "or" {
				want = []string{"first", "second"}
			}
			if got := MetaFilter(metadata, &MetaFilterInput{Conditions: conditions, Logic: logic}); !reflect.DeepEqual(got, want) {
				t.Fatalf("missing field %s/%s = %v, want %v", operator, logic, got, want)
			}
		}
	}
	conditions := []MetaCondition{{Key: "status", Operator: "=", Value: "absent"}, {Key: "version", Operator: "=", Value: "v2"}}
	if got := MetaFilter(metadata, &MetaFilterInput{Conditions: conditions, Logic: "and"}); len(got) != 0 {
		t.Fatalf("an empty first AND predicate reopened the scope: %v", got)
	}
	conditions[0].Value = "ready"
	if got := MetaFilter(metadata, &MetaFilterInput{Conditions: conditions, Logic: "and"}); !reflect.DeepEqual(got, []string{"second"}) {
		t.Fatalf("intersection = %v", got)
	}
	if got := MetaFilter(metadata, &MetaFilterInput{Conditions: conditions, Logic: "or"}); !reflect.DeepEqual(got, []string{"first", "second", "third"}) {
		t.Fatalf("union = %v", got)
	}
}

func TestInvalidMetadataMembershipInvalidatesWholeExpression(t *testing.T) {
	metadata := MetaData{"status": {"ready": {"selected"}, "other": {"outside"}}}
	operands := []interface{}{nil, 0, false, map[string]interface{}{}, []interface{}{nil}, []interface{}{map[string]interface{}{}}, []interface{}{[]string{"ready"}}, []interface{}{math.NaN()}, []interface{}{math.Inf(1)}, []interface{}{"ready", nil}}
	for _, operand := range operands {
		for _, logic := range []string{"and", "or"} {
			for _, operator := range []string{"in", "not in"} {
				conditions := []MetaCondition{{Key: "status", Operator: "is", Value: "ready"}, {Key: "status", Operator: operator, Value: operand}}
				if got := MetaFilter(metadata, &MetaFilterInput{Conditions: conditions, Logic: logic}); len(got) != 0 {
					t.Fatalf("invalid %s %T under %s returned %v", operator, operand, logic, got)
				}
			}
		}
	}
}

func TestParseAndConvertDoesNotDiscardMalformedPredicates(t *testing.T) {
	metadata := MetaData{"status": {"ready": {"selected"}}}
	valid := map[string]interface{}{"name": "status", "comparison_operator": "is", "value": "ready"}
	for _, malformed := range []interface{}{nil, "condition", map[string]interface{}{"comparison_operator": "is", "value": "ready"}, map[string]interface{}{"name": "status", "value": "ready"}} {
		input := ParseAndConvert(map[string]interface{}{"conditions": []interface{}{valid, malformed}, "logic": "or"})
		if input == nil {
			t.Fatal("malformed filter became absent")
		}
		if got := MetaFilter(metadata, input); len(got) != 0 {
			t.Fatalf("malformed OR predicate was discarded: %v", got)
		}
	}
	for _, input := range []map[string]interface{}{{"conditions": "bad"}, {"conditions": []interface{}{}, "logic": "xor"}, {"logic": 0}} {
		parsed := ParseAndConvert(input)
		if parsed == nil || len(MetaFilter(metadata, parsed)) != 0 {
			t.Fatalf("malformed filter was not retained as invalid: %v", input)
		}
	}
	if ParseAndConvert(nil) != nil || ParseAndConvert(map[string]interface{}{}) != nil || ParseAndConvert(map[string]interface{}{"conditions": []interface{}{}}) != nil {
		t.Fatal("absent/empty conditions must preserve the no-filter contract")
	}
	parsed := ParseAndConvert(map[string]interface{}{"conditions": []interface{}{valid}})
	if parsed == nil || parsed.Logic != "and" || parsed.Conditions[0].Operator != "=" {
		t.Fatalf("conversion = %#v", parsed)
	}
}
