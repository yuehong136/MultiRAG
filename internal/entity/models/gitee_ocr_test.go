package models

import (
	"context"
	"errors"
	"io"
	"net/http"

	"sync/atomic"
	"testing"
)

func TestGiteeOCRInputsAndFailures(t *testing.T) {
	var calls atomic.Int32
	response := `{"text_result":"识别🙂"}`
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Method != "POST" || r.URL.Path != "/v1/images/ocr" || r.Header.Get("Authorization") != "Bearer fixture" {
			t.Error("request contract")
		}
		if err := r.ParseMultipartForm(modelResponseLimitForTest); err != nil {
			t.Error(err)
			return
		}
		defer r.MultipartForm.RemoveAll()
		if r.FormValue("model") != "HunyuanOCR" {
			t.Error("model missing")
		}
		if image := r.FormValue("image"); image != "" {
			if image != "https://example.com/image.png" {
				t.Error("URL changed")
			}
		} else {
			f, _, err := r.FormFile("image")
			if err != nil {
				t.Error(err)
				return
			}
			defer f.Close()
			b, _ := io.ReadAll(f)
			if string(b) != "image bytes" {
				t.Error("bytes changed")
			}
		}
		_, _ = io.WriteString(w, response)
	}))
	defer srv.Close()
	driver := NewGiteeModel(map[string]string{"default": srv.URL + "/v1"}, URLSuffix{OCR: "images/ocr"})
	name, key := "HunyuanOCR", "fixture"
	api := &APIConfig{ApiKey: &key}
	for _, url := range []bool{false, true} {
		var result *OCRFileResponse
		var err error
		if url {
			result, err = driver.OCRFile(&name, nil, stringPtr("https://example.com/image.png"), api, nil)
		} else {
			result, err = driver.OCRFile(&name, []byte("image bytes"), nil, api, nil)
		}
		if err != nil || result.Text == nil || *result.Text != "识别🙂" {
			t.Fatalf("%v %v", result, err)
		}
	}
	before := calls.Load()
	for _, bad := range []string{"", "file:///tmp/a", "https://user:pass@example.com/a", "https://example.com/a#fragment"} {
		if _, err := driver.OCRFile(&name, nil, stringPtr(bad), api, nil); err == nil {
			t.Fatal("invalid URL accepted")
		}
	}
	if _, err := driver.OCRFile(&name, nil, nil, api, nil); err == nil {
		t.Fatal("empty file accepted")
	}
	if _, err := driver.OCRFile(&name, []byte("x"), nil, nil, nil); err == nil {
		t.Fatal("missing key accepted")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := driver.OCRFile(&name, []byte("x"), nil, &APIConfig{ApiKey: &key, Context: ctx}, nil); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if calls.Load() != before {
		t.Fatal("invalid request made external call")
	}
	for _, raw := range []string{`{}`, `{"text_result":null}`, `{"text_result":"x","error":"failed"}`, `{"text_result":"x"} {}`} {
		response = raw
		if _, err := driver.OCRFile(&name, []byte("image bytes"), nil, api, nil); err == nil {
			t.Fatal("malformed provider success", raw)
		}
	}
	response = `{"text_result":""}`
	if r, err := driver.OCRFile(&name, []byte("image bytes"), nil, api, nil); err != nil || *r.Text != "" {
		t.Fatal("blank page rejected", err)
	}
}
