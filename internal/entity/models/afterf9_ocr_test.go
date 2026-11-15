package models

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync/atomic"
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
	d := afterf9Driver(t, "PaddleOCR", srv.URL)
	if _, err := d.OCRFile(stringPtr("PaddleOCR-VL"), nil, stringPtr("https://invalid.example/doc.pdf"), nil, nil); err == nil {
		t.Fatal("local URL-only OCR accepted")
	}
	result, err := d.(ModelDriver).OCRFile(stringPtr("PaddleOCR-VL"), []byte("%PDF-input"), nil, nil, nil)
	if err != nil || result == nil || *result.Text != "page one\n\npage two" {
		t.Fatal(result, err)
	}
}

func TestPaddleOCRLocalKeylessInstanceEndpointIsolation(t *testing.T) {
	var requests atomic.Int32
	srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requestNumber := requests.Add(1)
		if r.Header.Get("Authorization") != "" {
			t.Error("local instance sent a credential")
		}
		expectedPath := "/instance/ocr"
		if requestNumber == 2 {
			expectedPath = "/catalog-private/ocr"
		}
		if requestNumber > 2 || r.URL.Path != expectedPath {
			t.Error("selected endpoint changed", r.URL.Path)
		}
		fmt.Fprint(w, `{"errorCode":0,"result":{"layoutParsingResults":[{"markdown":{"text":"local page"}}]}}`)
	}))
	defer srv.Close()
	catalogURLs := map[string]string{"default": srv.URL + "/catalog", "private": srv.URL + "/catalog-private"}
	catalog, err := NewModelFactory().CreateModelDriver("PaddleOCR", catalogURLs, URLSuffix{OCR: "ocr"})
	if err != nil {
		t.Fatal(err)
	}
	instanceURLs := map[string]string{"local": srv.URL + "/instance"}
	instance := catalog.NewInstance(instanceURLs)
	instanceURLs["local"] = srv.URL + "/mutated-instance"
	catalogURLs["private"] = srv.URL + "/mutated-catalog"
	for _, call := range []struct {
		driver ModelDriver
		region string
	}{{instance, "local"}, {catalog, "private"}} {
		result, callErr := call.driver.OCRFile(stringPtr("PaddleOCR-VL"), []byte("image"), nil, &APIConfig{ApiKey: stringPtr(""), Region: &call.region}, nil)
		if callErr != nil || result == nil || result.Text == nil || *result.Text != "local page" {
			t.Fatal(result, callErr)
		}
	}
	if requests.Load() != 2 {
		t.Fatal("instance and catalog did not use their own endpoints", requests.Load())
	}
	for _, call := range []struct {
		driver ModelDriver
		region string
	}{{catalog.NewInstance(nil), "default"}, {instance, "missing"}} {
		result, callErr := call.driver.OCRFile(stringPtr("PaddleOCR-VL"), []byte("image"), nil, &APIConfig{Region: &call.region}, nil)
		if callErr == nil || result != nil {
			t.Fatal("missing local endpoint accepted", result, callErr)
		}
	}
	if requests.Load() != 2 {
		t.Fatal("missing endpoint called a provider", requests.Load())
	}
}

func TestAfterf9OCRMalformedAndErrors(t *testing.T) {
	for _, provider := range []string{"PaddleOCR", "ZHIPU-AI"} {
		bodies := []string{`null`, `{}`, `{"error":"private-secret"}`, `{} {}`}
		if provider == "PaddleOCR" {
			bodies = append(bodies, `{"errorCode":1,"result":{"layoutParsingResults":[]}}`, `{"errorCode":null,"result":{"layoutParsingResults":[{"markdown":{"text":"ok"}}]}}`, `{"errorCode":0,"result":{"layoutParsingResults":[{}]}}`, `{"errorCode":0,"result":{"layoutParsingResults":[{"markdown":{"text":null}}]}}`)
		} else {
			bodies = append(bodies, `{"md_results":null}`, `{"md_results":5}`)
		}
		for _, body := range bodies {
			t.Run(provider+body, func(t *testing.T) {
				srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
				defer srv.Close()
				result, err := afterf9Driver(t, provider, srv.URL).(ModelDriver).OCRFile(stringPtr("served"), []byte("image"), nil, &APIConfig{ApiKey: stringPtr("key")}, nil)
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
	result, err := afterf9Driver(t, "ZHIPU-AI", srv.URL).(ModelDriver).OCRFile(stringPtr("glm-ocr"), []byte("%PDF-input"), nil, &APIConfig{ApiKey: stringPtr("key")}, nil)
	if err != nil || result == nil || *result.Text != "markdown" {
		t.Fatal(result, err)
	}
}

func TestAfterf9LocalPaddleBlankPage(t *testing.T) {
	srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"errorCode":0,"result":{"layoutParsingResults":[{"markdown":{"text":""}}]}}`)
	}))
	defer srv.Close()
	response, err := afterf9Driver(t, "PaddleOCR", srv.URL).(ModelDriver).OCRFile(stringPtr("PaddleOCR-VL"), []byte("image"), nil, nil, nil)
	if err != nil || response == nil || *response.Text != "" {
		t.Fatal("explicit blank page rejected", response, err)
	}
}
