package service

import (
	"strings"
	"testing"
)

func TestAfter10bbeThinkTagsAtEverySplit(t *testing.T) {
	raw := "before <think>private <think>nested</think> thought</think> visible <think>unclosed secret"
	for split := 0; split <= len(raw); split++ {
		state := &ThinkStreamState{}
		NextThinkDelta(state, raw[:split])
		NextThinkDelta(state, raw)
		var visible strings.Builder
		for _, d := range FlushThinkBuffer(state) {
			if d.Kind == ThinkDeltaText {
				visible.WriteString(d.Value)
			}
		}
		if visible.String() != "before  visible " {
			t.Fatalf("split %d visible %q", split, visible.String())
		}
	}
	for _, raw := range []string{"<thi", "<think>hidden", "<think>hidden</thi", "<THINK>hidden"} {
		if answer := ExtractVisibleAnswer(raw); answer != "" {
			t.Fatalf("private tag exposed %q", answer)
		}
	}
	if got := ExtractVisibleAnswer("<think>中文思考</think>可见答案"); got != "可见答案" {
		t.Fatal(got)
	}
}
