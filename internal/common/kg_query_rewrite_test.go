package common

import (
	"reflect"
	"strings"
	"testing"
)

func TestKGQueryRewritePromptPreservesValueBoundaries(t *testing.T) {
	question := "What is {TYPE_POOL}?\n{query} 🐈"
	pool := `{"PERSON":["{query}","a\\b"]}`
	prompt := BuildQueryRewritePrompt(question, pool)
	if !strings.Contains(prompt, "Query: "+question+"\nAnswer type pool:"+pool+"\n") {
		t.Fatalf("caller values were reinterpreted: %s", prompt)
	}
	if strings.Count(prompt, "-Real Data-") != 1 || !strings.Contains(prompt, "No more than 3.") || !strings.HasSuffix(prompt, "Output:\n") {
		t.Fatal("prompt instructions or template boundaries changed")
	}
}

func TestKGQueryRewriteValidWrappersAndLimits(t *testing.T) {
	object := `{"answer_type_keywords":["A","B","C","D"],"entities_from_query":["braces { } and \"quotes\"","雪","3","4","5","6"]}`
	want := &QueryRewriteResult{TypeKeywords: []string{"A", "B", "C"}, Entities: []string{`braces { } and "quotes"`, "雪", "3", "4", "5"}}
	for _, response := range []string{object, " \n" + object + "\n", "```json\n" + object + "\n```", "Here is the response:\n" + object + "\nDone."} {
		got, err := ParseQueryRewriteResponse(response)
		if err != nil || !reflect.DeepEqual(got, want) {
			t.Fatalf("parse(%q) = %#v, %v", response, got, err)
		}
	}
	got, err := ParseQueryRewriteResponse(`{"answer_type_keywords":[],"entities_from_query":[]}`)
	if err != nil || got.TypeKeywords == nil || got.Entities == nil || len(got.Entities) != 0 {
		t.Fatalf("explicit empty arrays = %#v, %v", got, err)
	}
}

func TestKGQueryRewriteRejectsMalformedAndAmbiguousResults(t *testing.T) {
	valid := `{"answer_type_keywords":[],"entities_from_query":[]}`
	responses := []string{
		"", "no JSON", "{broken}", "null", "[]", "{}", `{"answer_type_keywords":[]}`,
		`{"answer_type_keywords":null,"entities_from_query":[]}`,
		`{"answer_type_keywords":[],"entities_from_query":null}`,
		`{"answer_type_keywords":[],"entities_from_query":[null]}`,
		`{"answer_type_keywords":[],"entities_from_query":[" "]}`,
		`{"answer_type_keywords":[3],"entities_from_query":[]}`,
		`{"answer_type_keywords":"PERSON","entities_from_query":[]}`,
		`{"answer_type_keywords":[],"entities_from_query":{},"unknown":[]}`,
		`{"answer_type_keywords":[],"answer_type_keywords":["B"],"entities_from_query":[]}`,
		`{"answer_type_keywords":[],"entities_from_query":[],"unknown":[]}`,
		valid + valid, "```json\n" + valid + "\n```\n" + valid,
		`{"nested":` + valid + `}`, `{"nested":` + valid,
	}
	for _, response := range responses {
		if got, err := ParseQueryRewriteResponse(response); err == nil || got != nil {
			t.Fatalf("invalid response accepted: %q -> %#v, %v", response, got, err)
		}
	}
}
