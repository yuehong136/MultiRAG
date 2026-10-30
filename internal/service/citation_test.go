package service

import (
	"math"
	"strings"
	"testing"
)

func TestAfter10bbeCitationsPreserveCodeAndBindIDs(t *testing.T) {
	answer := "中文事实。\n```go\nfmt.Println(\"code.\")\n```\nEnglish fact."
	spans := citationSpans(answer)
	if len(spans) != 2 {
		t.Fatal(spans)
	}
	chunks := []SourcedChunk{{ID: "zh"}, {ID: "en"}, {ID: "zero"}}
	decorated, used, err := insertCitationsWithVectors(answer, spans, chunks, [][]float64{{1, 0}, {0, 1}}, [][]float64{{1, 0}, {0, 1}, {0, 0}})
	if err != nil || !used[0] || !used[1] || used[2] || !strings.Contains(decorated, "中文事实。 [ID:zh]") || !strings.Contains(decorated, "English fact. [ID:en]") || !strings.Contains(decorated, "```go\nfmt.Println(\"code.\")\n```") {
		t.Fatal(decorated, used, err)
	}
	clean, refs := cleanAnswerCitations(decorated+" [ID:foreign] [ID:unfinished", chunks, true)
	if strings.Contains(clean, "foreign") || strings.Contains(clean, "unfinished") || len(refs) != 2 {
		t.Fatal(clean, refs)
	}
	clean, refs = cleanAnswerCitations(decorated, chunks, false)
	if strings.Contains(clean, "[ID:") || len(refs) != 0 {
		t.Fatal(clean, refs)
	}
	for _, vector := range [][]float64{{1}, {math.NaN(), 1}, {math.Inf(1), 0}} {
		if _, _, err := insertCitationsWithVectors(answer, spans, chunks, [][]float64{{1, 0}, {0, 1}}, [][]float64{vector, nil, nil}); err == nil {
			t.Fatal("invalid citation vector accepted")
		}
	}
	if _, _, err := insertCitationsWithVectors(answer, spans, chunks, [][]float64{{1, 0}}, nil); err == nil {
		t.Fatal("misaligned batch accepted")
	}
	zero, used, err := insertCitationsWithVectors(answer, spans, chunks, [][]float64{{0, 0}, {0, 0}}, [][]float64{{1, 0}, nil, {0, 0}})
	if err != nil || zero != answer || len(used) != 0 {
		t.Fatal("zero vector fabricated citation", zero, used, err)
	}
	if len(citationSpans("```python\nunfinished code.")) != 0 {
		t.Fatal("unfinished fence cited")
	}
}
