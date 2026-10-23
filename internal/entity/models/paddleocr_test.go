package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestPaddleOCRJobHTTPContract(t *testing.T) {
	for _, input := range []string{"bytes", "url"} {
		t.Run(input, func(t *testing.T) {
			name, key := "PaddleOCR-VL-1.5", "controlled-paddle-key"
			var polls atomic.Int32
			var base string
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/result" {
					if r.Header.Get("Authorization") != "" || r.Header.Get("Cookie") != "" {
						t.Error("job credentials leaked into result download")
					}
					fmt.Fprintln(w, `{"result":{"layoutParsingResults":[{"markdown":{"text":"page zero"}},{"prunedResult":{"parsing_res_list":[{"block_content":"page one"}]}}]}}`)
					fmt.Fprintln(w, `{"result":{"ocrResults":[{"prunedResult":{"rec_texts":["line two","line three"]}}]}}`)
					return
				}
				if r.Header.Get("Authorization") != "Bearer "+key {
					t.Error("PaddleOCR job key lost")
				}
				switch r.URL.Path {
				case "/api/v2/ocr/jobs":
					if input == "url" {
						var body map[string]any
						if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
							t.Error(err)
						}
						if body["fileUrl"] != "https://files.invalid/document.pdf" || body["model"] != name {
							t.Error("Paddle URL/model lost")
						}
					} else {
						if err := r.ParseMultipartForm(1 << 20); err != nil {
							t.Error(err)
							return
						}
						defer r.MultipartForm.RemoveAll()
						file, _, err := r.FormFile("file")
						if err != nil {
							t.Error(err)
							return
						}
						defer file.Close()
						raw, _ := io.ReadAll(file)
						if string(raw) != "PDF bytes" || r.FormValue("model") != name || !strings.Contains(r.FormValue("optionalPayload"), `"useDocUnwarping":false`) {
							t.Error("Paddle multipart lost")
						}
					}
					fmt.Fprint(w, `{"data":{"jobId":"job-1"}}`)
				case "/api/v2/ocr/jobs/job-1":
					if polls.Add(1) == 1 {
						fmt.Fprint(w, `{"data":{"state":"running"}}`)
					} else {
						fmt.Fprintf(w, `{"data":{"state":"done","resultUrl":{"jsonUrl":%q}}}`, base+"/result?signature=controlled")
					}
				default:
					t.Error("wrong job path", r.URL.Path)
					w.WriteHeader(404)
				}
			}))
			defer srv.Close()
			base = srv.URL
			d := NewPaddleOCRModel(map[string]string{"default": srv.URL + "/api"}, URLSuffix{OCR: "v2/ocr/jobs"})
			d.pollInterval = time.Millisecond
			var result *OCRResponse
			var err error
			if input == "bytes" {
				result, err = d.OCRFile(&name, []byte("PDF bytes"), &APIConfig{APIKey: &key}, nil)
			} else {
				result, err = d.OCRURL(&name, "https://files.invalid/document.pdf", &APIConfig{APIKey: &key}, nil)
			}
			if err != nil || *result.Text != "page zero\n\npage one\n\nline two\nline three" || polls.Load() != 2 {
				t.Fatal(result, err, polls.Load())
			}
		})
	}
}

func TestPaddleOCRStrictJobFailures(t *testing.T) {
	for _, failure := range []string{"submit-http", "submit-json", "submit-provider", "missing-job", "unsafe-job", "poll-http", "poll-json", "unknown-state", "failed-state", "missing-url", "result-http", "result-json", "result-empty", "result-null", "truncated"} {
		t.Run(failure, func(t *testing.T) {
			name, key := "PP-OCRv5", "controlled-key"
			base := ""
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch r.URL.Path {
				case "/jobs":
					switch failure {
					case "submit-http":
						w.WriteHeader(502)
						fmt.Fprint(w, "private-body-"+key)
					case "submit-json":
						fmt.Fprint(w, "invalid")
					case "submit-provider":
						fmt.Fprint(w, `{"errorCode":9,"data":{"jobId":"job"}}`)
					case "missing-job":
						fmt.Fprint(w, `{"data":{}}`)
					case "unsafe-job":
						fmt.Fprint(w, `{"data":{"jobId":"../secret"}}`)
					default:
						fmt.Fprint(w, `{"data":{"jobId":"job"}}`)
					}
				case "/jobs/job":
					switch failure {
					case "poll-http":
						w.WriteHeader(502)
					case "poll-json":
						fmt.Fprint(w, "bad")
					case "unknown-state":
						fmt.Fprint(w, `{"data":{"state":"unknown"}}`)
					case "failed-state":
						fmt.Fprint(w, `{"data":{"state":"failed","errorMsg":"private-body"}}`)
					case "missing-url":
						fmt.Fprint(w, `{"data":{"state":"done"}}`)
					default:
						fmt.Fprintf(w, `{"data":{"state":"done","resultJsonUrl":%q}}`, base+"/result")
					}
				case "/result":
					switch failure {
					case "result-http":
						w.WriteHeader(502)
					case "result-json":
						fmt.Fprintln(w, "invalid")
					case "result-null":
						fmt.Fprintln(w, `{"result":{"ocrResults":[{"prunedResult":{"rec_texts":[null,"one"]}}]}}`)
					case "truncated":
						w.Header().Set("Content-Length", "1000")
						fmt.Fprint(w, `{"result":{}}`)
					default:
						fmt.Fprint(w, "")
					}
				default:
					w.WriteHeader(404)
				}
			}))
			defer srv.Close()
			base = srv.URL
			d := NewPaddleOCRModel(map[string]string{"default": srv.URL}, URLSuffix{OCR: "jobs"})
			if _, err := d.OCRFile(&name, []byte("bytes"), &APIConfig{APIKey: &key}, nil); err == nil || strings.Contains(err.Error(), "private-body") || strings.Contains(err.Error(), key) {
				t.Fatal("job failure accepted/leaked", err)
			}
		})
	}
}

func TestPaddleOCRBoundedPollingAndConfiguration(t *testing.T) {
	name, key := "PP-OCRv5", "controlled-key"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "POST" {
			fmt.Fprint(w, `{"data":{"jobId":"job"}}`)
		} else {
			fmt.Fprint(w, `{"data":{"state":"pending"}}`)
		}
	}))
	defer srv.Close()
	d := NewPaddleOCRModel(map[string]string{"default": srv.URL}, URLSuffix{OCR: "jobs"})
	d.pollInterval = time.Millisecond
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if _, err := d.OCRFile(&name, []byte("bytes"), &APIConfig{APIKey: &key, Context: ctx}, nil); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("Paddle deadline lost", err)
	}
	d.pollInterval = 0
	if _, err := d.OCRFile(&name, []byte("bytes"), &APIConfig{APIKey: &key}, nil); err == nil || !strings.Contains(err.Error(), "limit") {
		t.Fatal("polling bound lost", err)
	}
	for _, raw := range []string{`{bad`, `[]`, `{"api_key":null}`, `{"paddleocr_access_token":9}`, `{"paddleocr_access_token":"key","paddleocr_algorithm":"wrong"}`} {
		if _, _, err := d.jobConfig(name, &APIConfig{APIKey: &raw}); err == nil {
			t.Fatal("invalid config accepted", raw)
		}
	}
	for _, raw := range []string{`{"api_key":{"paddleocr_access_token":"token","paddleocr_api_url":"https://config.invalid/jobs"}}`, `{"PADDLEOCR_ACCESS_TOKEN":"token","PADDLEOCR_API_URL":"https://config.invalid/jobs"}`} {
		configured := NewPaddleOCRModel(map[string]string{"default": paddleOCRBaseURL}, URLSuffix{OCR: "v2/ocr/jobs"})
		endpoint, resolved, err := configured.jobConfig(name, &APIConfig{APIKey: &raw})
		if err != nil || endpoint != "https://config.invalid/jobs" || *resolved.APIKey != "token" {
			t.Fatal("provider JSON config lost", endpoint, err)
		}
		endpoint, _, err = d.jobConfig(name, &APIConfig{APIKey: &raw})
		if err != nil || endpoint != srv.URL+"/jobs" {
			t.Fatal("explicit instance endpoint priority lost", endpoint, err)
		}
	}
}

func TestPaddleOCRDownloadBoundaryAndJSONL(t *testing.T) {
	for _, raw := range []string{"", `{}`, `{"result":{}}`, `{"result":{"layoutParsingResults":[{}]}}`, `{"result":{"ocrResults":[{"prunedResult":{"rec_texts":null}}]}}`, `{"errorCode":9,"result":{"ocrResults":[{"prunedResult":{"rec_texts":["should fail"]}}]}}`, "{bad}\n"} {
		if _, err := readPaddleJSONL(strings.NewReader(raw)); err == nil {
			t.Fatal("bad JSONL accepted", raw)
		}
	}
	large := strings.Repeat("large ", 20000)
	raw := fmt.Sprintf(`{"result":{"layoutParsingResults":[{"markdown":{"text":%q}}]}}`, large)
	if result, err := readPaddleJSONL(strings.NewReader(raw)); err != nil || len(*result.Text) != len(strings.TrimSpace(large)) {
		t.Fatal("large valid JSONL truncated", err)
	}
	var destinationCalls atomic.Int32
	destination := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { destinationCalls.Add(1) }))
	defer destination.Close()
	var providerURL string
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" || r.Header.Get("Cookie") != "" {
			t.Error("result redirect leaked credentials")
		}
		if r.URL.Path == "/blocked" {
			http.Redirect(w, r, destination.URL+"/private?signature=secret", http.StatusFound)
			return
		}
		if r.URL.Path == "/redirect" {
			http.Redirect(w, r, providerURL+"/result", http.StatusFound)
			return
		}
		fmt.Fprint(w, `{"result":{"ocrResults":[{"prunedResult":{"rec_texts":["recognized"]}}]}}`)
	}))
	defer provider.Close()
	providerURL = provider.URL
	key := "do-not-forward-key"
	driver := NewPaddleOCRModel(map[string]string{"default": provider.URL}, URLSuffix{OCR: "jobs"})
	if result, err := driver.downloadResult(provider.URL+"/jobs", provider.URL+"/redirect", &APIConfig{APIKey: &key}); err != nil || *result.Text != "recognized" {
		t.Fatal(result, err)
	}
	if _, err := driver.downloadResult(provider.URL+"/jobs", provider.URL+"/blocked", &APIConfig{APIKey: &key}); err == nil || strings.Contains(err.Error(), "signature") || strings.Contains(err.Error(), key) {
		t.Fatal("unsafe redirect accepted/leaked", err)
	}
	if destinationCalls.Load() != 0 {
		t.Fatal("unsafe redirect reached private destination")
	}
	base, _ := url.Parse(provider.URL)
	for _, target := range []string{"file:///etc/passwd", "https://127.0.0.1/private", "http://169.254.169.254/meta", "https://user:secret@public.invalid/ocr", "https://[::1]/result"} {
		parsed, _ := url.Parse(target)
		if err := validatePaddleResultURL(base, parsed); err == nil {
			t.Fatal("unsafe result URL accepted", target)
		}
	}
	for _, address := range []string{"127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "::1", "fc00::1"} {
		if paddlePublicIP(net.ParseIP(address)) {
			t.Fatal("private result IP accepted", address)
		}
	}
	if !paddlePublicIP(net.ParseIP("8.8.8.8")) {
		t.Fatal("public result IP rejected")
	}
}
