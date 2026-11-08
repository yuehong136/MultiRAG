package chunk

import (
	"multirag/internal/testsupport"
	"testing"
)

func TestExpressionRejectsMalformedAndShortCircuits(t *testing.T) {
	testsupport.NewCase(t)
	for _, input := range []string{"", "length >", "(true", "true false", "length = 1.2.3", "name = 'unterminated", "@", "true AND"} {
		if _, err := CompileExpression(input); err == nil {
			t.Errorf("invalid expression accepted %q", input)
		}
	}
	for _, input := range []string{"false AND unknown = true", "true OR unknown = true", "NOT false AND length >= 2"} {
		result, err := Evaluate(input, map[string]any{"length": 2})
		if err != nil {
			t.Fatal(input, err)
		}
		if result != (input != "false AND unknown = true") {
			t.Fatal(input, result)
		}
	}
}
