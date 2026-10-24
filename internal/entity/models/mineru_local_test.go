package models

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
)

func TestMinerULocalMultipartAndResult(t *testing.T) {
	for _, key := range []string{"", " ", "key"} {
		t.Run(key, func(t *testing.T) {
			var calls atomic.Int32
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				wantAuth := ""
				if strings.TrimSpace(key) != "" {
					wantAuth = "Bearer " + key
				}
				if r.Header.Get("Authorization") != wantAuth {
					t.Error("incorrect optional key")
				}
				switch r.URL.Path {
				case "/file_parse":
					if r.Method != http.MethodPost || r.ParseMultipartForm(MaxMediaBytes) != nil {
						t.Error("multipart POST required")
					}
					file, header, err := r.FormFile("files")
					if err != nil {
						t.Error(err)
						return
					}
					defer file.Close()
					raw, err := io.ReadAll(file)
					if err != nil || string(raw) != "%PDF-local" || header.Filename != "upload_document.pdf" || r.FormValue("backend") != "pipeline" {
						t.Error("file/backend changed")
					}
					w.WriteHeader(http.StatusAccepted)
					fmt.Fprint(w, `{"data":{"task_id":"job-1"}}`)
				case "/tasks/job-1/result":
					if calls.Add(1) == 1 {
						w.WriteHeader(http.StatusAccepted)
						fmt.Fprint(w, `{"status":"running"}`)
					} else {
						fmt.Fprint(w, `{"results":{"z.pdf":{"md_content":"last"},"a.pdf":{"md_content":"first🙂"}}}`)
					}
				default:
					t.Error("unexpected path", r.URL.Path)
					w.WriteHeader(404)
				}
			}))
			defer srv.Close()
			base := map[string]string{"default": srv.URL}
			driver, err := NewModelFactory().CreateModelDriver("MINERU_LOCAL", base, URLSuffix{DocumentParse: "file_parse", Task: "tasks"})
			if err != nil || driver.Name() != "mineru_local" {
				t.Fatal(driver, err)
			}
			base["default"] = "http://changed.invalid"
			api := &APIConfig{APIKey: &key}
			task, err := driver.(FileParser).ParseFile(nil, []byte("%PDF-local"), "", api)
			if err != nil || task.TaskID != "job-1" || task.Status != "pending" || len(task.Segments) != 0 {
				t.Fatal(task, err)
			}
			reader := driver.(TaskReader)
			task, err = reader.ShowTask("job-1", api)
			if err != nil || task.Status != "running" || len(task.Segments) != 0 {
				t.Fatal("pending represented as document", task, err)
			}
			task, err = reader.ShowTask("job-1", api)
			if err != nil || task.Status != "success" || !reflect.DeepEqual(task.Segments, []TaskSegment{{1, "first🙂"}, {2, "last"}}) {
				t.Fatal(task, err)
			}
			if _, err := reader.ListTasks(api); !errors.Is(err, ErrUnsupportedCapability) {
				t.Fatal("task listing falsely supported", err)
			}
		})
	}
}

func TestMinerULocalRejectsInvalidResponses(t *testing.T) {
	for _, test := range []struct{ name, response string }{
		{"missing", `{}`}, {"null", `null`}, {"bad-json", `{"`}, {"trailing", `{"task_id":"job-1"} {}`},
		{"bad-id", `{"task_id":"../secret"}`}, {"null-id", `{"task_id":null}`},
		{"conflicting", `{"task_id":"one","data":{"task_id":"two"}}`},
		{"error", `{"task_id":"job-1","error":{"message":"private-secret"}}`},
		{"code", `{"task_id":"job-1","code":1}`}, {"failed", `{"task_id":"job-1","status":"failed"}`},
		{"results", `{"task_id":"job-1","results":{"doc":{"md_content":"early"}}}`},
	} {
		t.Run("submit/"+test.name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, test.response) }))
			defer srv.Close()
			m := NewMinerULocalModel(map[string]string{"default": srv.URL}, URLSuffix{DocumentParse: "file_parse"})
			if _, err := m.ParseFile(nil, []byte("pdf"), "", nil); err == nil || strings.Contains(err.Error(), "private-secret") {
				t.Fatal("accepted invalid response or leaked body", err)
			}
		})
	}
	for _, raw := range []string{`{}`, `{"results":{}}`, `{"results":null}`, `{"results":{"a":null}}`, `{"results":{"a":{"md_content":null}}}`, `{"results":{"a":{"md_content":" "}}}`, `{"results":{"a":{"md_content":1}}}`, `{"status":"running","results":{"a":{"md_content":"early"}}}`, `{"status":"failed"}`, `{"status":"cancelled"}`, `{"status":"unknown"}`, `{"task_id":"other","results":{"a":{"md_content":"text"}}}`} {
		t.Run("result/"+raw, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, raw) }))
			defer srv.Close()
			m := NewMinerULocalModel(map[string]string{"default": srv.URL}, URLSuffix{Task: "tasks"})
			if _, err := m.ShowTask("job-1", nil); err == nil {
				t.Fatal("invalid result accepted")
			}
		})
	}
}

func TestMinerULocalInputCancellationAndHTTP(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.WriteHeader(502)
		fmt.Fprint(w, "private-secret")
	}))
	defer srv.Close()
	m := NewMinerULocalModel(map[string]string{"default": srv.URL}, URLSuffix{DocumentParse: "file_parse", Task: "tasks"})
	if _, err := m.ParseFile(nil, nil, "", nil); err == nil {
		t.Fatal("empty file accepted")
	}
	if _, err := m.ParseFile(nil, []byte("pdf"), "https://files.invalid/a.pdf", nil); !errors.Is(err, ErrUnsupportedCapability) {
		t.Fatal(err)
	}
	if _, err := m.ShowTask("../job", nil); err == nil || calls.Load() != 0 {
		t.Fatal("invalid input caused request", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := m.ParseFile(nil, []byte("pdf"), "", &APIConfig{Context: ctx}); !errors.Is(err, context.Canceled) {
		t.Fatal("cancellation lost", err)
	}
	if _, err := m.ShowTask("job-1", nil); err == nil || strings.Contains(err.Error(), "private-secret") {
		t.Fatal("HTTP error or private body leaked", err)
	}
}

func TestMinerULocalAcceptedResponseMustBeObject(t *testing.T) {
	for _, raw := range []string{`null`, `[]`, `{"status":null}`, `{"task_id":null}`, `{"results":null}`, `{"data":null}`} {
		t.Run(raw, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(http.StatusAccepted)
				fmt.Fprint(w, raw)
			}))
			defer srv.Close()
			m := NewMinerULocalModel(map[string]string{"default": srv.URL}, URLSuffix{Task: "tasks"})
			if _, err := m.ShowTask("job-1", nil); err == nil {
				t.Fatal("malformed accepted response became pending")
			}
		})
	}
}
