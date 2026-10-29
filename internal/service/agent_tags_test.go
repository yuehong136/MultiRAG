package service

import (
	"strings"
	"testing"
	"unicode/utf8"
)

func TestNormalizeAgentTags(t *testing.T) {
	for _, tc := range []struct {
		raw  any
		want string
	}{
		{[]string{" ML ", "ml", "Data,Science", strings.Repeat("中", 70), strings.Repeat("中", 64)}, "ML,Data Science," + strings.Repeat("中", 64)},
		{" a, b, A,, ", "a,b"}, {[]string{}, ""}, {"", ""},
		{[]string{"İ", "i", "ΟΣ", "ος", "\x1c a \x1f"}, "İ,i,ΟΣ,a"},
	} {
		got, err := normalizeAgentTags(tc.raw)
		if err != nil || got != tc.want {
			t.Fatalf("normalization %v: %q %v", tc.raw, got, err)
		}
	}
	for _, raw := range []any{nil, 0, []any{"a"}, []int{1}, map[string]string{"a": "b"}} {
		if _, err := normalizeAgentTags(raw); err == nil {
			t.Fatalf("invalid type accepted: %T", raw)
		}
	}
	tags := []string{}
	for i := 0; i < 20; i++ {
		tags = append(tags, strings.Repeat(string(rune('a'+i)), 64))
	}
	got, err := normalizeAgentTags(tags)
	if err != nil || utf8.RuneCountInString(got) > 512 || len(strings.Split(got, ",")) != 7 {
		t.Fatal("whole-token storage cap", got, err)
	}
}
