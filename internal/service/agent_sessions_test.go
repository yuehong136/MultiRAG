package service

import (
	"encoding/json"
	"testing"
	"time"

	"multirag/internal/entity"
)

func TestAgentSessionNormalizationPreservesStoredContract(t *testing.T) {
	message := json.RawMessage(`[{"role":"assistant","prompt":"starter-private"},{"role":"user","prompt":"private-user"},{"role":"assistant","prompt":"private-answer","content":"answer","large_id":9007199254740993}]`)
	reference := json.RawMessage(`[{"chunks":[{"chunk_id":0,"id":"fallback","content_with_weight":"","content":"fallback","doc_type":"image","doc_type_kwd":["image"],"position_int":[[0,1,2]],"kb_id":9007199254740993}]}]`)
	session := &entity.API4Conversation{ID: "session", DialogID: "agent", Message: message, Reference: reference, Tokens: 9007199254740993, DSL: entity.JSONMap{"model_id": json.Number("9007199254740993")}}
	beforeMessage, beforeReference := string(message), string(reference)
	result := normalizeAgentSession(session, true)
	if result["agent_id"] != "agent" || result["tokens"] != int(9007199254740993) {
		t.Fatal(result)
	}
	for _, key := range []string{"messages", "reference", "dialog_id"} {
		if _, ok := result[key]; ok {
			t.Fatal("storage alias leaked", key)
		}
	}
	messages := result["message"].([]map[string]any)
	for _, message := range messages {
		if _, ok := message["prompt"]; ok {
			t.Fatal("prompt leaked")
		}
	}
	refs := messages[2]["reference"].([]map[string]interface{})
	if len(refs) != 1 || refs[0]["id"] != json.Number("0") || refs[0]["content"] != "" || refs[0]["dataset_id"] != json.Number("9007199254740993") {
		t.Fatal(refs)
	}
	if result["dsl"].(entity.JSONMap)["model_id"] != json.Number("9007199254740993") {
		t.Fatal(result["dsl"])
	}
	if string(message) != beforeMessage || string(reference) != beforeReference {
		t.Fatal("mutated storage")
	}
	result = normalizeAgentSession(session, false)
	if _, ok := result["dsl"]; ok {
		t.Fatal("dsl=false ignored")
	}
}

func TestAgentSessionReferenceAlignmentAndValidation(t *testing.T) {
	session := &entity.API4Conversation{Message: json.RawMessage(`[{"role":"assistant"},{"role":"user"},{"role":"assistant"},{"role":"user"},{"role":"assistant"}]`), Reference: json.RawMessage(`{"10":{"chunks":[{"id":"later"}]},"2":{"chunks":[{"id":"earlier"}]}}`)}
	result := normalizeAgentSession(session, true)
	messages := result["message"].([]map[string]interface{})
	for i, want := range map[int]string{2: "earlier", 4: "later"} {
		if got := messages[i]["reference"].([]map[string]interface{})[0]["id"]; got != want {
			t.Fatal(got, want)
		}
	}
	for _, bad := range []string{`{}`, `[null]`, `[1]`, `[`} {
		session.Message = json.RawMessage(bad)
		if normalizeAgentSession(session, true) != nil {
			t.Fatalf("invalid messages accepted: %s", bad)
		}
	}
}

func TestAgentSessionDateRangesAndValidation(t *testing.T) {
	start, err := parseAgentSessionDate("2026-10-29", false)
	if err != nil {
		t.Fatal(err)
	}
	end, err := parseAgentSessionDate("2026-10-29", true)
	if err != nil || !end.Equal(start.AddDate(0, 0, 1).Add(-time.Nanosecond)) {
		t.Fatal(end, err)
	}
	offsetDate, err := parseAgentSessionDate("2026-10-29T16:00:00-04:00", false)
	if err != nil || offsetDate.Location() != time.Local || !offsetDate.Equal(time.Date(2026, 10, 29, 20, 0, 0, 0, time.UTC)) {
		t.Fatal("offset date not normalized to local wall time", offsetDate, err)
	}
	for _, valid := range []string{"", "2026-10-29T23:59:59.999999+08:00", "2026-10-29 12:30:40"} {
		if _, err := parseAgentSessionDate(valid, false); err != nil {
			t.Fatal(valid, err)
		}
	}
	for _, bad := range []string{"bad", "2026-02-30", "2026-10-29T12:00:00", "2026-10-29T00:00:00Zjunk"} {
		if _, err := parseAgentSessionDate(bad, false); err == nil {
			t.Fatal(bad)
		}
	}
}

func TestAgentReferenceChunksRetainPrimitiveValues(t *testing.T) {
	refs := normalizeAgentReferenceChunks([]interface{}{"legacy", json.Number("0"), nil, false})
	if len(refs) != 4 || refs[0]["id"] != "legacy" || refs[0]["content"] != "legacy" || refs[1]["id"] != json.Number("0") || refs[1]["content"] != "0" || refs[2]["id"] != nil || refs[2]["content"] != "None" || refs[3]["id"] != false || refs[3]["content"] != "False" {
		t.Fatal("primitive chunk data lost", refs)
	}
	for _, ref := range refs {
		if _, exists := ref["citation_id"]; exists {
			t.Fatal("non-target citation protocol leaked", ref)
		}
	}
}
