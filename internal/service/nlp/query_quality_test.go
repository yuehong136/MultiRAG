package nlp

import (
	"reflect"
	"strings"
	"testing"
)

func TestQueryPrecisionAndSynonymEscaping(t *testing.T) {
	qb := &QueryBuilder{
		queryFields: []string{"content_ltks"},
		termWeight:  &TermWeightDealer{ne: map[string]string{}, df: map[string]int{}},
		synonym:     &Synonym{dictionary: map[string][]string{"alpha": {"a+b"}}},
	}
	query, keywords := qb.Question(strings.Repeat("alpha ", 30), "", .3)
	if query == nil || !strings.Contains(query.MatchingText, "^0.0333") || !strings.Contains(query.MatchingText, `"a\+b"^0.0083`) {
		t.Fatalf("low-weight terms or escaped synonym lost: %#v", query)
	}
	found := false
	for _, keyword := range keywords {
		if keyword == "a+b" {
			found = true
		}
	}
	if !found {
		t.Fatal("synonym missing from scoring/highlight keywords", keywords)
	}
	qb.synonym = &Synonym{dictionary: map[string][]string{}}
	query, _ = qb.Question(`alpha* beta~ gamma' delta" epsilon\`, "", .3)
	if strings.ContainsAny(query.MatchingText, "*~'\\") {
		t.Fatal("query punctuation leaked", query.MatchingText)
	}
}

func TestSynonymDictionaryResultsCannotMutateSharedSource(t *testing.T) {
	s := &Synonym{dictionary: map[string][]string{"name": {"a+b", "second"}}}
	first := s.Lookup("name", 1)
	first[0] = "escaped"
	second := s.Lookup("name", 8)
	second[1] = "changed"
	if got := s.Lookup("name", 8); !reflect.DeepEqual(got, []string{"a+b", "second"}) {
		t.Fatal("lookup mutated shared dictionary", got)
	}
}

func TestFineGrainedFrequencyIgnoresSingleRunes(t *testing.T) {
	if got := multiRuneTokens("中 中文 a alpha 1 12"); !reflect.DeepEqual(got, []string{"中文", "alpha", "12"}) {
		t.Fatal(got)
	}
}
