package models

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestBaiduMistralOCRHTTPContract(t *testing.T) {
	for _, provider := range []string{"Baidu", "Mistral"} {
		t.Run(provider, func(t *testing.T) {
			key, name := "controlled-ocr-key", "ocr-model"
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/v1/ocr" || r.Header.Get("Authorization") != "Bearer "+key {
					t.Error("OCR endpoint/key lost")
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				if body["model"] != name {
					t.Error("OCR model lost")
				}
				if provider == "Baidu" {
					if body["fileType"] != float64(0) {
						t.Error("PDF type lost")
					}
					input, _ := body["file"].(string)
					if !strings.HasPrefix(input, "https://") {
						raw, err := base64.StdEncoding.DecodeString(input)
						if err != nil || string(raw) != "%PDF-1.4 document" {
							t.Error("PDF bytes lost")
						}
					}
					fmt.Fprint(w, `{"result":{"layoutParsingResults":[{"markdown":{"text":"page zero"}},{"markdown":{"text":"page one"}}]}}`)
				} else {
					document, _ := body["document"].(map[string]any)
					if document["type"] != "document_url" {
						t.Error("document type lost")
					}
					input, _ := document["document_url"].(string)
					if !strings.HasPrefix(input, "https://") && !strings.HasPrefix(input, "data:application/pdf;base64,") {
						t.Error("document data URI lost", input)
					}
					fmt.Fprint(w, `{"pages":[{"index":1,"markdown":"page one"},{"index":0,"markdown":"page zero"}]}`)
				}
			}))
			defer srv.Close()
			var driver ModelDriver
			if provider == "Baidu" {
				driver = NewBaiduModel(map[string]string{"default": srv.URL + "/v1"}, URLSuffix{OCR: "ocr"})
			} else {
				driver = NewMistralModel(map[string]string{"default": srv.URL + "/v1"}, URLSuffix{OCR: "ocr"})
			}
			api := &APIConfig{APIKey: &key}
			for _, urlInput := range []bool{false, true} {
				var result *OCRResponse
				var err error
				if urlInput {
					result, err = driver.(OCRURLDriver).OCRURL(&name, "https://files.invalid/doc.PDF?download=1", api, nil)
				} else {
					result, err = driver.(OCRDriver).OCRFile(&name, []byte("%PDF-1.4 document"), api, nil)
				}
				if err != nil || *result.Text != "page zero\n\npage one" {
					t.Fatal("multi-page/order lost", result, err)
				}
			}
		})
	}
}

func TestBaiduMistralOCRStrictFailures(t *testing.T) {
	for provider, bodies := range map[string][]string{
		"Baidu":   {`{}`, `{"result":{"layoutParsingResults":[]}}`, `{"result":{"layoutParsingResults":[{}]}}`, `{"result":{"layoutParsingResults":[{"markdown":{"text":""}}]}}`, `{"error_code":99,"result":{}}`},
		"Mistral": {`{}`, `{"pages":[]}`, `{"pages":[{"markdown":"one"}]}`, `{"pages":[{"index":0,"markdown":null}]}`, `{"pages":[{"index":0,"markdown":"one"},{"index":0,"markdown":"two"}]}`, `{"pages":[{"index":0,"markdown":""}]}`, `{"error":"private-body"}`},
	} {
		for _, raw := range bodies {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, raw) }))
			var driver ModelDriver
			if provider == "Baidu" {
				driver = NewBaiduModel(map[string]string{"default": srv.URL}, URLSuffix{OCR: "ocr"})
			} else {
				driver = NewMistralModel(map[string]string{"default": srv.URL}, URLSuffix{OCR: "ocr"})
			}
			name, key := "ocr", "controlled-key"
			if _, err := driver.(OCRDriver).OCRFile(&name, []byte("bytes"), &APIConfig{APIKey: &key}, nil); err == nil || strings.Contains(err.Error(), "private-body") {
				t.Fatal("invalid OCR result accepted/leaked", provider, raw, err)
			}
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			if _, err := driver.(OCRDriver).OCRFile(&name, []byte("bytes"), &APIConfig{APIKey: &key, Context: ctx}, nil); err == nil {
				t.Fatal("OCR cancellation lost")
			}
			srv.Close()
		}
	}
}

func TestCoHereASRHTTPContract(t *testing.T) {
	name, key := "cohere-transcribe-03-2026", "controlled-key"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+key {
			t.Error("Cohere ASR credential lost")
		}
		reader, err := r.MultipartReader()
		if err != nil {
			t.Error(err)
			return
		}
		fields := map[string]string{}
		fileSeen := false
		for {
			part, err := reader.NextPart()
			if err == io.EOF {
				break
			}
			if err != nil {
				t.Error(err)
				return
			}
			raw, _ := io.ReadAll(part)
			if part.FormName() == "file" {
				fileSeen = true
				if fields["model"] != name || fields["language"] != "en" || string(raw) != "audio bytes" || part.FileName() != "audio.wav" {
					t.Error("Cohere ASR file/field order lost")
				}
			} else {
				if fileSeen {
					t.Error("Cohere ASR option after file")
				}
				fields[part.FormName()] = string(raw)
			}
		}
		fmt.Fprint(w, `{"text":"transcription"}`)
	}))
	defer srv.Close()
	d := NewCoHereModel(map[string]string{"default": srv.URL}, URLSuffix{ASR: "audio/transcriptions"})
	if result, err := d.TranscribeAudio(&name, []byte("audio bytes"), &APIConfig{APIKey: &key}, &ASRConfig{Language: []string{"en"}}); err != nil || *result.Text != "transcription" {
		t.Fatal(result, err)
	}
}
