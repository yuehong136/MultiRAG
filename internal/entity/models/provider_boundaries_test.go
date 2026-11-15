package models

import (
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
)

func TestZhipuBlankKeyDenialHasNoHTTP(t *testing.T) {
	var calls atomic.Int32
	srv := providerTestServer(t, http.HandlerFunc(func(http.ResponseWriter, *http.Request) { calls.Add(1) }))
	d := NewZhipuAIModel(map[string]string{"default": srv.URL}, URLSuffix{Chat: "chat", Files: "files"})
	for _, api := range []*APIConfig{nil, {}, {ApiKey: stringPtr(" \t ")}} {
		if result, err := d.ChatWithMessages("glm", []Message{{Role: "user", Content: "q"}}, api, nil); err == nil || result != nil {
			t.Fatal("blank chat key accepted")
		}
		if err := d.ChatStreamlyWithSender("glm", []Message{{Role: "user", Content: "q"}}, api, nil, func(*string, *string) error { return nil }); err == nil {
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
			if !(err != nil) || !strings.Contains(err.Error(), strings.ToLower(provider)) {
				t.Fatal(provider, action, err)
			}
		}

		if _, err := d.ParseFile(nil, nil, nil, nil, nil); err == nil {
			t.Fatal("unsupported parser accepted")
		}
		if _, err := d.ListTasks(nil); err == nil {
			t.Fatal("unsupported tasks accepted")
		}
		if provider != "Mistral" {
			if _, err := d.OCRFile(nil, nil, nil, nil, nil); err == nil {
				t.Fatal("unsupported OCR accepted")
			}
		}

	}
	if calls.Load() != 0 {
		t.Fatal("unsupported capability reached provider")
	}
}
