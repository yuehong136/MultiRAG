package handler

import "testing"

func TestParseMemoryMessagePath(t *testing.T) {
	for _, value := range []string{"memory:0", "memory_1-2:42", "m:9223372036854775807"} {
		memory, id, err := parseMemoryMessagePath(value)
		if err != nil || memory == "" || id < 0 {
			t.Fatal(value, memory, id, err)
		}
	}
	for _, value := range []string{"", ":0", "m:", "m:-1", "m:+1", "m: 1", " m:1", "m:1 ", "m:1:2", "m:1.0", "m:9223372036854775808", "m/other:1", "m:\uFF10"} {
		if _, _, err := parseMemoryMessagePath(value); err == nil {
			t.Fatal("malformed message path accepted", value)
		}
	}
}
