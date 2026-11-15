package handler_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"multirag/internal/cli"
	"multirag/internal/testsupport"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
)

func TestMediaHTTPCLIAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	t.Run("OpenAI/speech2text", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalMedia(t, f, "OpenAI", "whisper-1", instance, "speech2text") })
		}
	})
	t.Run("OpenAI/tts", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalMedia(t, f, "OpenAI", "tts-1", instance, "tts") })
		}
	})
	t.Run("Gitee/ocr", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalMedia(t, f, "Gitee", "HunyuanOCR", instance, "ocr") })
		}
	})
	t.Run("ZHIPU-AI/ocr", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalMedia(t, f, "ZHIPU-AI", "glm-ocr", instance, "ocr") })
		}
	})
	t.Run("PaddleOCR/ocr", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalMedia(t, f, "PaddleOCR", "PaddleOCR-VL", instance, "ocr") })
		}
	})
	t.Run("Baidu/ocr", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalMedia(t, f, "Baidu", "owned-ocr", instance, "ocr") })
		}
	})
	t.Run("302.AI/ocr", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalMedia(t, f, "302.AI", "mistral-ocr-latest", instance, "ocr") })
		}
	})
	t.Run("MinerU/parse", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalMedia(t, f, "MinerU.Net", "vlm", instance, "parse") })
		}
	})
	t.Run("302.AI/parse", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalMedia(t, f, "302.AI", "vlm", instance, "parse") })
		}
	})
}

func TestLocalKeylessParserHTTPCLIAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	for _, instance := range []string{"one", "two"} {
		t.Run(instance, func(t *testing.T) { exerciseCanonicalLocalParser(t, f, instance) })
	}
}

func TestRoot55NativeMediaHTTPCLIAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	for _, tc := range []struct{ provider, model, kind string }{{"xAI", "grok-asr", "speech2text"}, {"xAI", "eve", "tts"}, {"Mistral", "mistral-ocr-latest", "ocr"}, {"MinerU.Net", "vlm", "parse"}} {
		t.Run(tc.provider+"/"+tc.kind, func(t *testing.T) {
			for _, instance := range []string{"one", "two"} {
				t.Run(instance, func(t *testing.T) { exerciseCanonicalMedia(t, f, tc.provider, tc.model, instance, tc.kind) })
			}
		})
	}
}

func TestRoot55HostedPaddleOCRHTTPCLIAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	for _, instance := range []string{"one", "two"} {
		t.Run(instance, func(t *testing.T) {
			var calls atomic.Int64
			var mode atomic.Int32
			var baseURL string
			key := f.Prefix + "paddle-net-" + instance
			input := []byte("owned image bytes")
			want := "page one " + instance + "\n\npage two " + instance
			supplier := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.URL.Path == "/result" {
					if r.Header.Get("Authorization") != "" {
						t.Error("job credentials leaked to artifact download")
					}
					parts := strings.Split(want, "\n\n")
					fmt.Fprintf(w, "{\"errorCode\":0,\"result\":{\"layoutParsingResults\":[{\"markdown\":{\"text\":%q}},{\"markdown\":{\"text\":%q}}]}}\n", parts[0], parts[1])
					return
				}
				if r.Header.Get("Authorization") != "Bearer "+key {
					t.Error("stored hosted OCR key crossed instances")
				}
				if r.Method == "GET" && r.URL.Path == "/v2/ocr/jobs/owned-job" {
					fmt.Fprintf(w, `{"data":{"state":"done","resultUrl":{"jsonUrl":%q}}}`, baseURL+"/result")
					return
				}
				if r.Method != "POST" || r.URL.Path != "/v2/ocr/jobs" {
					t.Error("native hosted OCR path/method changed")
				}
				if mode.Load() == 1 {
					w.WriteHeader(503)
					fmt.Fprint(w, "private-provider-body")
					return
				}
				if mode.Load() == 2 {
					fmt.Fprint(w, `{}`)
					return
				}
				if err := r.ParseMultipartForm(1 << 20); err != nil {
					t.Error(err)
					return
				}
				defer r.MultipartForm.RemoveAll()
				file, header, err := r.FormFile("file")
				if err != nil {
					t.Error(err)
					return
				}
				raw, _ := io.ReadAll(file)
				file.Close()
				if !bytes.Equal(raw, input) || header.Filename != "document.pdf" || r.FormValue("model") != "PP-OCRv5" {
					t.Error("native hosted OCR uploaded bytes/model changed")
				}
				fmt.Fprint(w, `{"data":{"jobId":"owned-job"}}`)
			}))
			baseURL = supplier.URL
			id := f.enroll(t, "PaddleOCR.Net", instance, key, baseURL, "PP-OCRv5", "ocr")
			before := f.snapshot(t)
			body := map[string]any{"model_id": id, "content": input}
			result := f.requireSuccess(t, "owner", "POST", "/api/v1/file/ocr", body)
			if result["data"].(map[string]any)["text"] != want {
				t.Fatal("hosted submit/poll/download lost pages")
			}
			directory := filepath.Join(os.TempDir(), f.Prefix+"-paddle-net-"+instance)
			f.Journal.Event("reserve-directory", map[string]any{"path": directory})
			if err := os.Mkdir(directory, 0700); err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() {
				if err := os.RemoveAll(directory); err != nil {
					t.Error(err)
				}
				_, err := os.Stat(directory)
				absent := os.IsNotExist(err)
				f.Journal.Event("cleanup-directory", map[string]any{"path": directory, "absent": absent})
				if !absent {
					t.Error("hosted OCR input remains")
				}
			})
			file := filepath.Join(directory, "owned.png")
			f.Journal.Event("reserve-file", map[string]any{"path": file})
			if err := os.WriteFile(file, input, 0600); err != nil {
				t.Fatal(err)
			}
			cmd, err := cli.NewParser(fmt.Sprintf("OCR WITH '%s' FILE '%s';", id, file)).Parse(cli.APIMode)
			if err != nil {
				t.Fatal(err)
			}
			response, err := f.cli(t, "owner").ExecuteCommand(cmd)
			if err != nil {
				t.Fatal("actual hosted OCR CLI", err)
			}
			raw, _ := json.Marshal(response)
			encoded, _ := json.Marshal(want)
			if !strings.Contains(string(raw), string(encoded)) {
				t.Fatal("CLI hosted OCR lost actual text")
			}
			if calls.Load() != 6 {
				t.Fatal("native submit/poll/download stages incomplete")
			}
			count := calls.Load()
			for _, who := range []string{"", "foreign", "inactive", "revoked", "invited", "member", "disabledtenant"} {
				_, failed, _ := f.request(t, who, "POST", "/api/v1/file/ocr", body)
				if providerEnvelopeError(failed) == nil {
					t.Fatal("denied hosted OCR accepted")
				}
			}
			if _, err = f.cli(t, "foreign").ExecuteCommand(cmd); err == nil {
				t.Fatal("foreign CLI hosted OCR succeeded")
			}
			if calls.Load() != count || f.snapshot(t) != before {
				t.Fatal("denied hosted OCR SQL/provider effects")
			}
			for _, value := range []int32{1, 2} {
				mode.Store(value)
				_, failed, raw := f.request(t, "owner", "POST", "/api/v1/file/ocr", body)
				if providerEnvelopeError(failed) == nil || strings.Contains(raw, "private-provider-body") {
					t.Fatal("bad hosted OCR submission succeeded/leaked")
				}
			}
			if f.snapshot(t) != before {
				t.Fatal("hosted OCR changed SQL")
			}
		})
	}
}
