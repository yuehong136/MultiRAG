package cli

import (
	"encoding/json"
	"testing"
)

func TestNumericModelSelectorPreservesInt64(t *testing.T) {
	for _, value := range []string{"1", "9007199254740993", "9223372036854775807"} {
		payload, err := modelSelectionPayload(value)
		if err != nil {
			t.Fatal(err)
		}
		if _, ok := payload["model_id"].(int64); !ok {
			t.Fatal("numeric ID lost integer precision")
		}
	}
	for _, value := range []string{"0", "-1", "01", "+1", "1.0", "9223372036854775808", "ffffffffffffffffffffffffffffffff", "a@@b", " a@b@c"} {
		if _, err := modelSelectionPayload(value); err == nil {
			t.Fatal("invalid selector", value)
		}
	}
	for _, input := range []string{`CHAT WITH '101' MESSAGE 'hello';`, `EMBED TEXT 'hello' WITH '101';`, `ASR WITH '101' AUDIO 'test.wav';`, `TTS WITH '101' TEXT 'hello';`, `OCR WITH '101' FILE 'test.png';`, `PARSE WITH '101' FILE 'test.pdf';`, `SET DEFAULT LLM '101';`} {
		if _, err := NewParser(input).Parse(false); err != nil {
			t.Fatal(input, err)
		}
	}
}

func TestDefaultModelsRejectsIncompleteEnvelope(t *testing.T) {
	for _, raw := range []string{`{}`, `{"code":0}`, `{"code":0,"data":null}`, `{"code":0,"data":{"models":null}}`, `{"code":0,"data":[]}`} {
		var result DefaultModelsResponse
		if err := json.Unmarshal([]byte(raw), &result); err == nil {
			t.Fatal("incomplete default response succeeded", raw)
		}
	}
	var result DefaultModelsResponse
	if err := json.Unmarshal([]byte(`{"code":0,"data":{"models":[]}}`), &result); err != nil || result.Data.Models == nil {
		t.Fatal("valid empty inventory failed", err)
	}
}
