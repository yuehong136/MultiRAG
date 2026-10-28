package models

import (
	"errors"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
)

func TestZhipuBlankKeyDenialHasNoHTTP(t *testing.T) {
	var calls atomic.Int32
	srv := providerTestServer(t, http.HandlerFunc(func(http.ResponseWriter, *http.Request) { calls.Add(1) }))
	d := NewZhipuAIModel(map[string]string{"default": srv.URL}, URLSuffix{Chat: "chat", Files: "files"})
	for _, api := range []*APIConfig{nil, {}, {APIKey: stringPtr(" \t ")}} {
		if result, err := d.ChatWithMessages("glm", api, []Message{{Role: "user", Content: "q"}}, nil); err == nil || result != nil {
			t.Fatal("blank chat key accepted")
		}
		if err := d.ChatStreamlyWithMessages("glm", []Message{{Role: "user", Content: "q"}}, api, nil, func(*string, *string) error { return nil }); err == nil {
			t.Fatal("blank stream key accepted")
		}
		if err := d.CheckConnection(api); err == nil {
			t.Fatal("blank probe key accepted")
		}
	}
	if calls.Load() != 0 {
		t.Fatal("denial reached provider")
	}
}

func TestUnsupportedCapabilitiesPreserveProviderAndNoHTTP(t *testing.T) {
	var calls atomic.Int32
	srv := providerTestServer(t, http.HandlerFunc(func(http.ResponseWriter, *http.Request) { calls.Add(1) }))
	for _, provider := range []string{"Mistral", "XunFei"} {
		d, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": srv.URL}, URLSuffix{})
		if err != nil {
			t.Fatal(err)
		}
		for _, action := range []string{"balance", "rerank", "embedding"} {
			if provider == "Mistral" && action == "embedding" {
				continue
			}
			var err error
			switch action {
			case "balance":
				_, err = d.Balance(nil)
			case "rerank":
				_, err = d.Rerank(nil, "", nil, nil, nil)
			default:
				_, err = d.Embed(nil, nil, nil, nil)
			}
			if !errors.Is(err, ErrUnsupportedCapability) || !strings.Contains(err.Error(), strings.ToLower(provider)) || !strings.Contains(err.Error(), action) {
				t.Fatal(provider, action, err)
			}
		}
		if _, ok := d.(ASRDriver); ok {
			t.Fatal("unexpected ASR capability")
		}
		if _, ok := d.(TTSDriver); ok {
			t.Fatal("unexpected TTS capability")
		}
		if _, ok := d.(FileParser); ok {
			t.Fatal("unexpected parser capability")
		}
		if _, ok := d.(TaskReader); ok {
			t.Fatal("unexpected task capability")
		}
		_, ocr := d.(OCRDriver)
		if ocr != (provider == "Mistral") {
			t.Fatal("real Mistral OCR capability changed")
		}
	}
	if calls.Load() != 0 {
		t.Fatal("unsupported capability reached provider")
	}
}
