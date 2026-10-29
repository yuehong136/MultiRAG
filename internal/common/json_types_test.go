package common

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestStringSlicePreservesScopePresence(t *testing.T) {
	var omitted struct {
		IDs StringSlice `json:"ids"`
	}
	if err := json.Unmarshal([]byte(`{}`), &omitted); err != nil || omitted.IDs != nil {
		t.Fatal("omission must remain nil", err)
	}
	for _, tc := range []struct {
		raw  string
		want StringSlice
	}{
		{`"kb"`, StringSlice{"kb"}}, {`["a","b"]`, StringSlice{"a", "b"}}, {`[]`, StringSlice{}}, {`""`, StringSlice{""}},
	} {
		var got StringSlice
		if err := json.Unmarshal([]byte(tc.raw), &got); err != nil || !reflect.DeepEqual(got, tc.want) {
			t.Fatalf("%s: %v %v", tc.raw, got, err)
		}
		encoded, err := json.Marshal(got)
		var roundtrip StringSlice
		if err != nil || json.Unmarshal(encoded, &roundtrip) != nil || !reflect.DeepEqual(roundtrip, tc.want) {
			t.Fatalf("roundtrip %s", encoded)
		}
	}
	for _, raw := range []string{`null`, `1`, `true`, `{}`, `[null]`, `[1]`, `["a",false]`} {
		got := StringSlice{"unchanged"}
		if err := json.Unmarshal([]byte(raw), &got); err == nil || !reflect.DeepEqual(got, StringSlice{"unchanged"}) {
			t.Fatalf("invalid %s mutated/accepted: %v", raw, got)
		}
	}
}
