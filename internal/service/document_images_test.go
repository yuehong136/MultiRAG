package service

import (
	"bytes"
	"encoding/base64"
	"golang.org/x/image/bmp"
	"image"
	"image/gif"
	"image/jpeg"
	"image/png"
	"testing"
)

func TestRasterMIMEValidatesBytes(t *testing.T) {
	img := image.NewRGBA(image.Rect(0, 0, 2, 2))
	for format, encode := range map[string]func(*bytes.Buffer) error{"png": func(b *bytes.Buffer) error { return png.Encode(b, img) }, "jpeg": func(b *bytes.Buffer) error { return jpeg.Encode(b, img, nil) }, "gif": func(b *bytes.Buffer) error { return gif.Encode(b, img, nil) }, "bmp": func(b *bytes.Buffer) error { return bmp.Encode(b, img) }} {
		var b bytes.Buffer
		if err := encode(&b); err != nil {
			t.Fatal(err)
		}
		want := "image/" + format
		if got, err := rasterMIME(b.Bytes()); err != nil || got != want {
			t.Fatal(format, got, err)
		}
		if _, err := rasterMIME(b.Bytes()[:len(b.Bytes())/2]); err == nil {
			t.Fatal("accepted truncated", format)
		}
	}
	webp, _ := base64.StdEncoding.DecodeString("UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA")
	if got, err := rasterMIME(webp); err != nil || got != "image/webp" {
		t.Fatal("webp", got, err)
	}
	for _, raw := range [][]byte{nil, []byte("<svg></svg>"), []byte("\x89PNG\r\n\x1a\ninvalid")} {
		if _, err := rasterMIME(raw); err == nil {
			t.Fatal("accepted invalid bytes")
		}
	}
}
