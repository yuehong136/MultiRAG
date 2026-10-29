package service

import (
	"encoding/json"
	"testing"
	"time"

	"multirag/internal/dao"
	"multirag/internal/entity"
)

func TestAgentSessionNormalizationPreservesStoredContract(t *testing.T) {
	message := json.RawMessage(`[{"role":"assistant","prompt":"starter-private"},{"role":"user","prompt":"private-user"},{"role":"assistant","prompt":"private-answer","content":"answer","large_id":9007199254740993}]`)
	reference := json.RawMessage(`[{"chunks":{"17":{"chunk_id":0,"id":"fallback","content_with_weight":"","content":"fallback","doc_type":"image","doc_type_kwd":["image"],"position_int":[[0,1,2]],"kb_id":9007199254740993},"2":"legacy-id","3":0,"4":null,"5":false,"6":[],"7":1.2,"8":"  "}}]`)
	session := &dao.AgentSession{API4Conversation: entity.API4Conversation{ID: "session", DialogID: "agent", Message: message, Reference: reference, Tokens: 9007199254740993}, DSL: json.RawMessage(`{"model_id":9007199254740993}`)}
	beforeMessage, beforeReference := string(message), string(reference)
	result, err := normalizeAgentSession(session, true)
	if err != nil {
		t.Fatal(err)
	}
	if result["agent_id"] != "agent" || result["tokens"] != json.Number("9007199254740993") {
		t.Fatal(result)
	}
	for _, key := range []string{"message", "reference", "dialog_id"} {
		if _, ok := result[key]; ok {
			t.Fatal("storage alias leaked", key)
		}
	}
	messages := result["messages"].([]map[string]any)
	for _, message := range messages {
		if _, ok := message["prompt"]; ok {
			t.Fatal("prompt leaked")
		}
	}
	refs := messages[2]["reference"].([]map[string]any)
	if len(refs) != 3 {
		t.Fatal("primitive normalization", refs)
	}
	mapped := refs[0]
	if mapped["citation_id"] != "17" || mapped["id"] != json.Number("0") || mapped["content"] != "" || mapped["doc_type"] != "image" || mapped["dataset_id"] != json.Number("9007199254740993") {
		t.Fatal(mapped)
	}
	if refs[1]["id"] != "legacy-id" || refs[2]["id"] != json.Number("0") || refs[2]["content"] != "0" {
		t.Fatal(refs)
	}
	if result["dsl"].(map[string]any)["model_id"] != json.Number("9007199254740993") {
		t.Fatal(result["dsl"])
	}
	if string(message) != beforeMessage || string(reference) != beforeReference {
		t.Fatal("mutated storage")
	}
	result, err = normalizeAgentSession(session, false)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := result["dsl"]; ok {
		t.Fatal("dsl=false ignored")
	}
}

func TestAgentSessionReferenceAlignmentAndValidation(t *testing.T) {
	session := &dao.AgentSession{API4Conversation: entity.API4Conversation{Message: json.RawMessage(`[{"role":"assistant"},{"role":"user"},{"role":"assistant"},{"role":"user"},{"role":"assistant"}]`), Reference: json.RawMessage(`{"10":{"reference":["later"]},"2":{"chunks":["earlier"]}}`)}}
	result, err := normalizeAgentSession(session, true)
	if err != nil {
		t.Fatal(err)
	}
	messages := result["messages"].([]map[string]any)
	for i, want := range map[int]string{2: "earlier", 4: "later"} {
		if got := messages[i]["reference"].([]map[string]any)[0]["id"]; got != want {
			t.Fatal(got, want)
		}
	}
	for _, bad := range []string{`{}`, `[null]`, `[1]`, `[`} {
		session.Message = json.RawMessage(bad)
		if _, err := normalizeAgentSession(session, true); err == nil {
			t.Fatalf("invalid stored messages accepted: %s", bad)
		}
	}
	session.Message = json.RawMessage(`[]`)
	session.Reference = json.RawMessage(`{"bad":{"chunks":[]}}`)
	if _, err := normalizeAgentSession(session, true); err == nil {
		t.Fatal("invalid turn reference accepted")
	}
	session.Reference = json.RawMessage(`[]`)
	result, err = normalizeAgentSession(session, true)
	if err != nil || len(result["messages"].([]map[string]any)) != 0 {
		t.Fatal(result, err)
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
	for _, raw := range []json.RawMessage{json.RawMessage(`null`), json.RawMessage(`{"chunks":[]}`)} {
		if _, err := decodeAgentSessionJSON(raw); err != nil {
			t.Fatal(err)
		}
	}
}

func TestAgentSessionSparseListCitationsKeepSourcePositions(t *testing.T) {
	for _, raw := range []string{`[null,"legacy",false,0]`, `["first",[],"third",null]`} {
		decoded, err := decodeAgentSessionJSON(json.RawMessage(raw))
		if err != nil {
			t.Fatal(err)
		}
		refs := normalizeAgentReferenceChunks(decoded)
		switch raw {
		case `[null,"legacy",false,0]`:
			if len(refs) != 2 || refs[0]["citation_id"] != "1" || refs[1]["citation_id"] != "3" || refs[1]["id"] != json.Number("0") {
				t.Fatal(refs)
			}
		default:
			if len(refs) != 2 || refs[0]["citation_id"] != "0" || refs[1]["citation_id"] != "2" {
				t.Fatal(refs)
			}
		}
	}
	decoded, _ := decodeAgentSessionJSON(json.RawMessage(`["first",0,null]`))
	refs := normalizeAgentReferenceChunks(decoded)
	for _, ref := range refs {
		if _, exists := ref["citation_id"]; exists {
			t.Fatal("contiguous legacy shape changed", refs)
		}
	}
}
