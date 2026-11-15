package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"

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
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
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

			var result *OCRFileResponse
			var err error
			if input == "bytes" {
				result, err = d.OCRFile(&name, []byte("PDF bytes"), nil, &APIConfig{ApiKey: &key}, nil)
			} else {
				result, err = d.OCRFile(&name, nil, stringPtr("https://files.invalid/document.pdf"), &APIConfig{ApiKey: &key}, nil)
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
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
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
			if _, err := d.OCRFile(&name, []byte("bytes"), nil, &APIConfig{ApiKey: &key}, nil); err == nil || strings.Contains(err.Error(), "private-body") || strings.Contains(err.Error(), key) {
				t.Fatal("job failure accepted/leaked", err)
			}
		})
	}
}

func TestPaddleOCRBoundedPollingAndConfiguration(t *testing.T) {
	name, key := "PP-OCRv5", "controlled-key"
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "POST" {
			fmt.Fprint(w, `{"data":{"jobId":"job"}}`)
		} else {
			fmt.Fprint(w, `{"data":{"state":"pending"}}`)
		}
	}))
	defer srv.Close()
	d := NewPaddleOCRModel(map[string]string{"default": srv.URL}, URLSuffix{OCR: "jobs"})

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if _, err := d.OCRFile(&name, []byte("bytes"), nil, &APIConfig{ApiKey: &key, Context: ctx}, nil); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("Paddle deadline lost", err)
	}

}

func TestPaddleOCRDownloadBoundaryAndJSONL(t *testing.T) {
	for i, raw := range []string{"", `{}`, `{"result":{}}`, `{"result":{"layoutParsingResults":[{}]}}`, `{"result":{"ocrResults":[{"prunedResult":{"rec_texts":null}}]}}`, `{"errorCode":9,"result":{"ocrResults":[{"prunedResult":{"rec_texts":["should fail"]}}]}}`, "{bad}\n"} {
		t.Run(fmt.Sprint("JSONL", i), func(t *testing.T) {
			t.Parallel()
			if _, err := paddleJSONLForTest(t, raw); err == nil {
				t.Fatal("bad JSONL accepted", raw)
			}
		})
	}
	t.Run("large valid", func(t *testing.T) {
		t.Parallel()
		large := strings.Repeat("large ", 20000)
		raw := fmt.Sprintf(`{"result":{"layoutParsingResults":[{"markdown":{"text":%q}}]}}`, large)
		if result, err := paddleJSONLForTest(t, raw); err != nil || result == nil || result.Text == nil || len(*result.Text) != len(strings.TrimSpace(large)) {
			t.Fatal("large JSONL truncated", err)
		}
	})
	t.Run("same origin redirect", func(t *testing.T) {
		t.Parallel()
		result, err := paddleResultForTest(t, "/redirect", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/redirect" {
				http.Redirect(w, r, "/result", http.StatusFound)
				return
			}
			fmt.Fprint(w, `{"result":{"ocrResults":[{"prunedResult":{"rec_texts":["recognized"]}}]}}`)
		}))
		if err != nil || result == nil || *result.Text != "recognized" {
			t.Fatal(result, err)
		}
	})
	t.Run("cross origin redirect", func(t *testing.T) {
		t.Parallel()
		var calls atomic.Int32
		destination := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1) }))
		_, err := paddleResultForTest(t, "/blocked", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			http.Redirect(w, r, destination.URL+"/private?signature=secret", http.StatusFound)
		}))
		if err == nil || strings.Contains(err.Error(), "signature") || strings.Contains(err.Error(), "fixture-key") || calls.Load() != 0 {
			t.Fatal("unsafe redirect reached destination or leaked credentials", calls.Load(), err)
		}
	})
	targets := []string{"file:///etc/passwd", "https://127.0.0.1/private", "http://169.254.169.254/meta", "https://user:secret@public.invalid/ocr", "https://[::1]/result"}
	for _, address := range []string{"10.0.0.1", "100.64.0.1", "fc00::1"} {
		targets = append(targets, "https://"+net.JoinHostPort(address, "443")+"/result")
	}
	for i, target := range targets {
		t.Run(fmt.Sprint("unsafe target", i), func(t *testing.T) {
			t.Parallel()
			_, err := paddleResultForTest(t, target, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { t.Error("unsafe target unexpectedly fetched") }))
			if err == nil || strings.Contains(err.Error(), "secret") {
				t.Fatal("unsafe target accepted/leaked", err)
			}
		})
	}
}

func paddleJSONLForTest(t *testing.T, raw string) (*OCRFileResponse, error) {
	t.Helper()
	return paddleResultForTest(t, "", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, raw) }))
}

// Drive the actual canonical submit/poll/download function; this fixture does not
// parse JSONL or validate a result URL on the driver's behalf.
func paddleResultForTest(t *testing.T, target string, result http.Handler) (*OCRFileResponse, error) {
	t.Helper()
	base := ""
	server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/jobs":
			fmt.Fprint(w, `{"data":{"jobId":"job"}}`)
		case "/jobs/job":
			endpoint := target
			if endpoint == "" {
				endpoint = base + "/result"
			} else if strings.HasPrefix(endpoint, "/") {
				endpoint = base + endpoint
			}
			json.NewEncoder(w).Encode(map[string]any{"data": map[string]any{"state": "done", "resultUrl": map[string]any{"jsonUrl": endpoint}}})
		default:
			if r.Header.Get("Authorization") != "" || r.Header.Get("Cookie") != "" {
				t.Error("job credentials leaked to result")
			}
			result.ServeHTTP(w, r)
		}
	}))
	base = server.URL
	ctx, cancel := context.WithTimeout(t.Context(), 7*time.Second)
	defer cancel()
	return NewPaddleOCRModel(map[string]string{"default": base}, URLSuffix{OCR: "jobs"}).OCRFile(stringPtr("PaddleOCR-VL"), []byte("PDF bytes"), nil, &APIConfig{ApiKey: stringPtr("fixture-key"), Context: ctx}, nil)
}
