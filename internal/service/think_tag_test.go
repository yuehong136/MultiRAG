package service

import (
	"strings"
	"testing"
)

func TestAfter10bbeThinkTagsAtEverySplit(t *testing.T) {
	raw := "before <think>private <think>nested</think> thought</think> visible <think>unclosed secret"
	for split := 0; split <= len(raw); split++ {
		var parser ThinkStreamState
		a, r := parser.Feed(raw[:split])
		b, s := parser.Feed(raw[split:])
		parser.Finish()
		if a+b != "before  visible " || r+s != "private nested thoughtunclosed secret" {
			t.Fatalf("split %d: visible=%q reasoning=%q", split, a+b, r+s)
		}
		if strings.Contains(a, "private") || strings.Contains(b, "secret") {
			t.Fatal("private content escaped")
		}
	}
	var parser ThinkStreamState
	var visible, thought strings.Builder
	for _, r := range "<think>中文思考</think>可见答案" {
		a, b := parser.Feed(string(r))
		visible.WriteString(a)
		thought.WriteString(b)
	}
	if visible.String() != "可见答案" || thought.String() != "中文思考" {
		t.Fatal(visible.String(), thought.String())
	}
	for _, raw := range []string{"<thi", "<think>hidden", "<think>hidden</thi", "<THINK>hidden"} {
		if answer := ExtractVisibleAnswer(raw); answer != "" {
			t.Fatalf("unclosed private tag exposed %q", answer)
		}
	}
}
