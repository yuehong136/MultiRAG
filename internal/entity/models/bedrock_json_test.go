package models

import (
	"io"
	"net/http"
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
		driver := NewBedrockModel(nil, URLSuffix{})
		driver.baseModel.httpClient.Transport = roundTripperFunc(func(*http.Request) (*http.Response, error) {
			return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(raw))}, nil
		})
		if _, err := driver.Embed(stringPtr("amazon.titan-embed-text-v2:0"), []string{"input"}, bedrockTestAPI(), nil); err == nil {
			t.Fatal("ambiguous JSON accepted")
		}
	}
}

func TestBedrockCredentialJSONRejectsAmbiguousMembers(t *testing.T) {
	for _, duplicate := range []string{`"bedrock_sk":"private-secret"`, `"BEDROCK_SK":"private-secret"`, `"bedrock_s\u006b":"private-secret"`} {
		key := strings.TrimSuffix(bedrockTestKey, "}") + "," + duplicate + "}"
		if _, err := parseBedrockKey(key); err == nil || strings.Contains(err.Error(), "private-secret") {
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
		{strings.Repeat("\x00", modelResponseLimitForTest/6)},
		{strings.Repeat("&", modelResponseLimitForTest/6)},
		{"a" + strings.Repeat("\u2028", modelResponseLimitForTest/6)},
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
