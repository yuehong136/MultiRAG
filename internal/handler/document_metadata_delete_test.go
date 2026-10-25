package handler

import (
	"reflect"
	"strings"
	"testing"
)

func TestMetadataDeletionSelectionIsExplicit(t *testing.T) {
	for _, body := range []string{`null`, `[]`, `{}`, `{"doc_id":""}`, `{"doc_id":"d","keys":""}`, `{"doc_id":"d","keys":null}`, `{"doc_id":"d","keys":"null"}`, `{"doc_id":"d","keys":"[]"}`, `{"doc_id":"d","keys":"[1]"}`, `{"doc_id":"d","keys":"[\"\"]"}`, `{"doc_id":"d","keys":"[\"a\",\"a\"]"}`, `{"doc_id":"d","keys":[]}`, `{"doc_id":"d","doc_id":"other"}`, `{"doc_id":"d","all":true}`, `{"doc_id":"d"} {}`} {
		if _, _, err := decodeMetadataDeletion(strings.NewReader(body)); err == nil {
			t.Fatal("ambiguous deletion accepted", body)
		}
	}
	doc, keys, err := decodeMetadataDeletion(strings.NewReader(`{"doc_id":"d"}`))
	if err != nil || doc != "d" || keys != nil {
		t.Fatal(doc, keys, err)
	}
	doc, keys, err = decodeMetadataDeletion(strings.NewReader(`{"doc_id":"d","keys":"[\"a.b'[]\",\"zero\"]"}`))
	if err != nil || doc != "d" || !reflect.DeepEqual(keys, []string{"a.b'[]", "zero"}) {
		t.Fatal(doc, keys, err)
	}
}
