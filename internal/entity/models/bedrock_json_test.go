package models

import (
	"strings"
	"testing"
)

func TestBedrockJSONRejectsAmbiguousMembers(t *testing.T) {
	for _, raw := range []string{
		`{"embedding":[null],"embedding":[0,1]}`,
		`{"embedding":[null],"Embedding":[0,1]}`,
		`{"embedding":[null],"embedd\u0069ng":[0,1]}`,
		`{"embeddings":{"float":[[0]],"float":[[1]]}}`,
		`{"embeddings":{"float":[[0]],"FLOAT":[[1]]}}`,
		`{"embeddings":{"float":[[0]],"Float":[[1]]}}`,
		`{"embeddings":[{"embedding":[1],"Embedding":[2]}]}`,
		`{"key":"a","\u212aey":"b"}`, // Unicode simple-fold collision with K.
		`{"embedding":[1]} {"embedding":[2]}`,
		`{"embedding":[1]} trailing`,
		`{"embedding":"` + string([]byte{255}) + `"}`,
	} {
		var target any
		if err := decodeBedrockJSON(strings.NewReader(raw), &target); err == nil {
			t.Fatal("ambiguous JSON accepted")
		}
	}
	var target any
	if err := decodeBedrockJSON(strings.NewReader(`{"a":[{"key":0},{"key":1}]}`), &target); err != nil {
		t.Fatal("members in separate objects conflicted", err)
	}
}

func TestBedrockCredentialJSONRejectsAmbiguousMembers(t *testing.T) {
	for _, duplicate := range []string{`"bedrock_sk":"private-secret"`, `"BEDROCK_SK":"private-secret"`, `"bedrock_s\u006b":"private-secret"`} {
		key := strings.TrimSuffix(bedrockTestKey, "}") + "," + duplicate + "}"
		if _, _, err := parseBedrockKey(&APIConfig{APIKey: &key}); err == nil || strings.Contains(err.Error(), "private-secret") {
			t.Fatal(err)
		}
	}
}

func TestBedrockEmbeddingOversizePrecedesCredentialAndHTTP(t *testing.T) {
	model := "cohere.embed-v4:0"
	for _, texts := range [][]string{
		// Repeated references keep test input small; marshaling this would allocate
		// more than 28 MB before the old post-Marshal size check could run.
		makeBedrockOversizeInputs(strings.Repeat("a", 300000), 96),
		{strings.Repeat("\x00", MaxMediaBytes/6)},
		{strings.Repeat("&", MaxMediaBytes/6)},
		{"a" + strings.Repeat("\u2028", MaxMediaBytes/6)},
	} {
		data, err := NewBedrockModel(nil, URLSuffix{}).Embed(&model, texts, nil, nil)
		if err == nil || data != nil || !strings.Contains(err.Error(), "oversized embedding request") {
			t.Fatal("preflight failed to reject before credential I/O", err)
		}
	}
}

func makeBedrockOversizeInputs(text string, count int) []string {
	texts := make([]string, count)
	for index := range texts {
		texts[index] = text
	}
	return texts
}
