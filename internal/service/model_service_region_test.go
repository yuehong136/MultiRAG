package service

import "testing"

func TestEncodeModelInstanceExtraDefaultsRegion(t *testing.T) {
	extra, err := encodeModelInstanceExtra("  ")
	if err != nil {
		t.Fatalf("encodeModelInstanceExtra() error = %v", err)
	}
	if extra != `{"region":"default"}` {
		t.Fatalf("encodeModelInstanceExtra() = %q", extra)
	}
}

func TestDecodeModelInstanceRegion(t *testing.T) {
	tests := []struct {
		name  string
		extra string
		want  string
	}{
		{name: "legacy empty value", extra: "", want: "default"},
		{name: "legacy empty object", extra: `{}`, want: "default"},
		{name: "configured region", extra: `{"region":"global"}`, want: "global"},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := decodeModelInstanceRegion(test.extra)
			if err != nil {
				t.Fatalf("decodeModelInstanceRegion() error = %v", err)
			}
			if got != test.want {
				t.Fatalf("decodeModelInstanceRegion() = %q, want %q", got, test.want)
			}
		})
	}
}

func TestDecodeModelInstanceRegionRejectsInvalidJSON(t *testing.T) {
	if _, err := decodeModelInstanceRegion("not-json"); err == nil {
		t.Fatal("decodeModelInstanceRegion() error = nil")
	}
}
