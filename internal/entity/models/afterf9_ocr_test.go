package models

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"
)

func TestAfterf9LocalPaddleOCRHTTP(t *testing.T) {
	srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/ocr" || r.Header.Get("Authorization") != "" {
			t.Error("local no-key request changed")
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		if body["fileType"] != float64(0) || body["file"] != base64.StdEncoding.EncodeToString([]byte("%PDF-input")) || body["model"] != nil {
			t.Error(body)
		}
		fmt.Fprint(w, `{"errorCode":0,"result":{"layoutParsingResults":[{"markdown":{"text":"page one"}},{"markdown":{"text":"page two"}}]}}`)
	}))
	defer srv.Close()
	d := afterf9Driver(t, "PaddleOCR_Local", srv.URL)
	if _, ok := d.(OCRURLDriver); ok {
		t.Fatal("local OCR must not gain URL fetch capability")
	}
	result, err := d.(OCRDriver).OCRFile(stringPtr("PaddleOCR-VL"), []byte("%PDF-input"), nil, nil)
	if err != nil || result == nil || *result.Text != "page one\n\npage two" {
		t.Fatal(result, err)
	}
}

func TestAfterf9OCRMalformedAndErrors(t *testing.T) {
	for _, provider := range []string{"PaddleOCR_Local", "ZHIPU-AI"} {
		bodies := []string{`null`, `{}`, `{"error":"private-secret"}`, `{} {}`}
		if provider == "PaddleOCR_Local" {
			bodies = append(bodies, `{"errorCode":1,"result":{"layoutParsingResults":[]}}`, `{"errorCode":null,"result":{"layoutParsingResults":[{"markdown":{"text":"ok"}}]}}`, `{"errorCode":0,"result":{"layoutParsingResults":[{}]}}`, `{"errorCode":0,"result":{"layoutParsingResults":[{"markdown":{"text":null}}]}}`, `{"errorCode":0,"result":{"layoutParsingResults":[]}}`)
		} else {
			bodies = append(bodies, `{"md_results":null}`, `{"md_results":5}`)
		}
		for _, body := range bodies {
			t.Run(provider+body, func(t *testing.T) {
				srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
				defer srv.Close()
				result, err := afterf9Driver(t, provider, srv.URL).(OCRDriver).OCRFile(stringPtr("served"), []byte("image"), &APIConfig{APIKey: stringPtr("key")}, nil)
				if err == nil || result != nil || strings.Contains(err.Error(), "private-secret") {
					t.Fatal(result, err)
				}
			})
		}
	}
}

func TestAfterf9ZhipuOCRHTTP(t *testing.T) {
	srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/ocr" || r.Header.Get("Authorization") != "Bearer key" {
			t.Error("request lost")
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		if body["model"] != "glm-ocr" || body["file"] != "data:application/pdf;base64,"+base64.StdEncoding.EncodeToString([]byte("%PDF-input")) {
			t.Error(body)
		}
		fmt.Fprint(w, `{"md_results":"markdown"}`)
	}))
	defer srv.Close()
	result, err := afterf9Driver(t, "ZHIPU-AI", srv.URL).(OCRDriver).OCRFile(stringPtr("glm-ocr"), []byte("%PDF-input"), &APIConfig{APIKey: stringPtr("key")}, nil)
	if err != nil || result == nil || *result.Text != "markdown" {
		t.Fatal(result, err)
	}
}

func TestAfterf9LocalPaddleBlankPage(t *testing.T) {
	srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"errorCode":0,"result":{"layoutParsingResults":[{"markdown":{"text":""}}]}}`)
	}))
	defer srv.Close()
	response, err := afterf9Driver(t, "PaddleOCR_Local", srv.URL).(OCRDriver).OCRFile(stringPtr("PaddleOCR-VL"), []byte("image"), nil, nil)
	if err != nil || response == nil || response.Text == nil || *response.Text != "" {
		t.Fatal("explicit blank page rejected", response, err)
	}
}
