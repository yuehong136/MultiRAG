package service

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

func TestAfter10bbeKnowledgeBudgetAndProjection(t *testing.T) {
	rows := []map[string]interface{}{{"chunk_id": "one", "kb_id": "kb", "doc_id": "doc", "docnm_kwd": "Report, Q1.pdf", "content_with_weight": "完整文本\nwith code ```x```", "positions": []any{[]any{1, 2, 3, 4, 5}}, "document_metadata": map[string]any{"z": false, "a": json.Number("9007199254740993"), "zero": 0}, "row_id": 0, "doc_type_kwd": "table", "vector": []float64{1, 0}, "secret_engine_field": "private"}}
	chunks, err := NewSourcedChunks(rows)
	if err != nil {
		t.Fatal(err)
	}
	first, selected, err := buildKnowledgePrompt(chunks, 10000, func(text string) (int, error) { return len(text), nil })
	if err != nil || len(selected) != 1 {
		t.Fatal(first, err)
	}
	if !json.Valid([]byte(first)) || !strings.Contains(first, `9007199254740993`) || strings.Contains(first, `"vector":`) || strings.Contains(first, "secret_engine") {
		t.Fatal(first)
	}
	again, _, _ := buildKnowledgePrompt(chunks, 10000, func(text string) (int, error) { return len(text), nil })
	if first != again {
		t.Fatal("unstable metadata ordering")
	}
	for _, budget := range []int{0, 1, len(first) - 1} {
		text, kept, err := buildKnowledgePrompt(chunks, budget, func(text string) (int, error) { return len(text), nil })
		if err != nil || text != "" || len(kept) != 0 {
			t.Fatal("partial structure exceeded budget", text, err)
		}
	}
	injected := errors.New("tokenizer failed")
	if _, _, err := buildKnowledgePrompt(chunks, 10, func(string) (int, error) { return 0, injected }); !errors.Is(err, injected) {
		t.Fatal("tokenizer error swallowed", err)
	}
	if _, _, err := buildKnowledgePrompt(chunks, 10, func(string) (int, error) { return 0, nil }); err == nil {
		t.Fatal("zero token success")
	}
	// Projection is independently owned and preserves complete positions/numbers.
	rows[0]["document_metadata"].(map[string]any)["z"] = true
	if chunks[0].DocumentMetadata.(map[string]any)["z"] != false || len(chunks[0].Positions.([]any)[0].([]any)) != 5 {
		t.Fatal("projection lost shape or aliased source")
	}
	if chunks[0].RowID != json.Number("0") || chunks[0].DocType != "table" {
		t.Fatal("structured row identity/type lost", chunks[0])
	}
	refs := answerReference(chunks)
	raw, _ := json.Marshal(refs)
	if strings.Contains(string(raw), "vector\"") || !strings.Contains(string(raw), `"count":1`) {
		t.Fatal(string(raw))
	}
}

func TestAfter10bbeChunkProjectionRejectsAmbiguousIDs(t *testing.T) {
	for _, rows := range [][]map[string]interface{}{
		{{"content": "x"}}, {{"id": "bad]", "content": "x"}}, {{"id": "dup", "content": "one"}, {"id": "dup", "content": "two"}},
	} {
		if _, err := NewSourcedChunks(rows); err == nil {
			t.Fatal("ambiguous citation accepted", rows)
		}
	}
	for _, value := range []any{nil, 0, "true", []any{}} {
		if _, err := answerCitationSetting(map[string]any{"quote": value}, "quote", true); err == nil {
			t.Fatal("invalid citation setting accepted")
		}
	}
}
