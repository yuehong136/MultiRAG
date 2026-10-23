package service

import (
	"encoding/binary"
	"reflect"
	"testing"
)

func TestDocumentMetadataStrictDecoding(t *testing.T) {
	for _, value := range []any{nil, map[string]any{}, "{}", "null", []byte{}, []byte("{}"), []byte("null")} {
		if result, err := decodeDocumentMetadata(value); err != nil || len(result) != 0 {
			t.Fatal("valid empty rejected", value, result, err)
		}
	}
	for _, value := range []any{"{bad", "", `[]`, `"text"`, "1", "false", 123, []any{}, []byte("{bad"), []byte{1, 0, 0}, []byte{8, 0, 0, 0, '{', '}'}} {
		if _, err := decodeDocumentMetadata(value); err == nil {
			t.Fatal("bad metadata accepted", value)
		}
	}
	prefix := func(value string) []byte {
		b := make([]byte, 4)
		binary.LittleEndian.PutUint32(b, uint32(len(value)))
		return append(b, []byte(value)...)
	}
	valid := append(prefix(`{"tag":["a"],"score":0}`), prefix(`{"tag":["b"],"score":1}`)...)
	result, err := decodeDocumentMetadata(valid)
	if err != nil || !reflect.DeepEqual(result["tag"], []any{"a", "b"}) || !reflect.DeepEqual(result["score"], []any{float64(0), float64(1)}) {
		t.Fatal("length-prefixed merge/zero contract lost", result, err)
	}
	for _, suffix := range [][]byte{{1}, prefix(`{bad}`), prefix(`[]`)} {
		if _, err := decodeDocumentMetadata(append(append([]byte{}, valid...), suffix...)); err == nil {
			t.Fatal("partial malformed suffix accepted")
		}
	}
}
