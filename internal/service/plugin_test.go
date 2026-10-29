package service

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestPluginServiceEmbeddedMetadata(t *testing.T) {
	got := NewPluginService().ListLLMTools()
	want := []LLMToolMetadata{{
		Name: "bad_calculator", DisplayName: "$t:bad_calculator.name",
		Description:        "A tool to calculate the sum of two numbers (will give wrong answer)",
		DisplayDescription: "$t:bad_calculator.description",
		Parameters: map[string]LLMToolParameter{
			"a": {Type: "number", Description: "The first number", DisplayDescription: "$t:bad_calculator.params.a", Required: true},
			"b": {Type: "number", Description: "The second number", DisplayDescription: "$t:bad_calculator.params.b", Required: true},
		},
	}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("embedded metadata mismatch: got=%+v want=%+v", got, want)
	}
	encoded, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	var raw []map[string]json.RawMessage
	if err := json.Unmarshal(encoded, &raw); err != nil {
		t.Fatal(err)
	}
	if len(raw[0]) != 5 || raw[0]["displayName"] == nil || raw[0]["displayDescription"] == nil {
		t.Fatalf("metadata fields differ from Python: %s", encoded)
	}
}

func TestPluginServiceMetadataIsIsolated(t *testing.T) {
	svc := NewPluginService()
	first := svc.ListLLMTools()
	first[0].Name = "tenant-private-tool"
	first[0].Parameters["a"] = LLMToolParameter{Type: "secret"}
	delete(first[0].Parameters, "b")
	first = append(first, LLMToolMetadata{Name: "injected"})
	second := svc.ListLLMTools()
	if len(second) != 1 || second[0].Name != "bad_calculator" || len(second[0].Parameters) != 2 || second[0].Parameters["a"].Type != "number" {
		t.Fatalf("request mutation leaked into later listing: %+v", second)
	}
}
