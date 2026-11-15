package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestMinerUTaskLifecycle(t *testing.T) {
	key, name := "controlled-key", "vlm"
	state := "pending"
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+key {
			t.Error("wrong authentication")
		}
		switch r.URL.Path {
		case "/api/v4/extract/task":
			var body map[string]any
			if r.Method != http.MethodPost || json.NewDecoder(r.Body).Decode(&body) != nil || body["url"] != "https://files.invalid/document.pdf" || body["model_version"] != name {
				t.Error("incorrect URL submission", body)
			}
			fmt.Fprint(w, `{"code":0,"data":{"task_id":"job-1"}}`)
		case "/api/v4/extract/task/job-1":
			if r.Method != http.MethodGet || r.ContentLength != 0 {
				t.Error("task query must be bodyless GET")
			}
			fmt.Fprintf(w, `{"code":0,"data":{"task_id":"job-1","state":%q,"full_zip_url":"https://files.invalid/result.zip"}}`, state)
		default:
			t.Error("unexpected path", r.URL.Path)
			w.WriteHeader(404)
		}
	}))
	defer srv.Close()
	driver, err := NewModelFactory().CreateModelDriver("MinerU.Net", map[string]string{"default": srv.URL}, URLSuffix{DocumentParse: "v4/extract/task"})
	if err != nil || driver.Name() != "mineru.net" {
		t.Fatal(driver, err)
	}
	m := driver.(*MinerUModel)
	empty := ""
	api := &APIConfig{ApiKey: &key, Region: &empty}
	task, err := m.ParseFile(&name, nil, stringPtr("https://files.invalid/document.pdf"), api, nil)
	if err != nil || task.TaskID != "job-1" {
		t.Fatal(task, err)
	}
	for _, next := range []string{"pending", "running", "converting", "done"} {
		state = next
		task, err := m.ShowTask("job-1", api)
		if state != "done" {
			if err == nil || task != nil {
				t.Fatal("unfinished task represented as document", task, err)
			}
			continue
		}
		if err != nil || len(task.Segments) != 1 || task.Segments[0].Index != 0 || task.Segments[0].Content != "https://files.invalid/result.zip" {
			t.Fatal("artifact URL lost", task, err)
		}
	}

	if _, err := m.ListTasks(api); !(err != nil) {
		t.Fatal("task listing must be unsupported", err)
	}
	if err := m.CheckConnection(api); err == nil {
		t.Fatal("unsupported connection check reported success")
	}
}

func TestMinerURejectsMalformedTasks(t *testing.T) {
	key, name := "controlled-key", "vlm"
	for _, tc := range []struct {
		name, response string
		query          bool
	}{
		{"missing-code", `{"data":{"task_id":"job-1"}}`, false},
		{"null-code", `{"code":null,"data":{"task_id":"job-1"}}`, false},
		{"bad-code", `{"code":1,"msg":"private-provider-body"}`, false},
		{"missing-id", `{"code":0,"data":{}}`, false},
		{"unsafe-id", `{"code":0,"data":{"task_id":"../secret"}}`, false},
		{"error-envelope", `{"code":0,"error":"private-provider-body","data":{"task_id":"job-1"}}`, false},
		{"trailing-json", `{"code":0,"data":{"task_id":"job-1"}} {}`, false},
		{"query-missing-code", `{"data":{"task_id":"job-1","state":"pending"}}`, true},
		{"query-missing-id", `{"code":0,"data":{"state":"pending"}}`, true},
		{"query-wrong-id", `{"code":0,"data":{"task_id":"other","state":"pending"}}`, true},
		{"query-unknown-state", `{"code":0,"data":{"task_id":"job-1","state":"new"}}`, true},
		{"query-failed", `{"code":0,"data":{"task_id":"job-1","state":"failed","err_msg":"private-provider-body"}}`, true},
		{"query-empty-artifact", `{"code":0,"data":{"task_id":"job-1","state":"done"}}`, true},
		{"query-invalid-artifact", `{"code":0,"data":{"task_id":"job-1","state":"done","full_zip_url":"file:///private"}}`, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, tc.response) }))
			defer srv.Close()
			m := NewMinerUModel(map[string]string{"default": srv.URL}, URLSuffix{DocumentParse: "task"})
			var err error
			if tc.query {
				_, err = m.ShowTask("job-1", &APIConfig{ApiKey: &key})
			} else {
				_, err = m.ParseFile(&name, nil, stringPtr("https://files.invalid/doc.pdf"), &APIConfig{ApiKey: &key}, nil)
			}
			if err == nil || strings.Contains(err.Error(), "private-provider-body") {
				t.Fatal("invalid task accepted or provider body leaked", err)
			}
		})
	}
}

func TestMinerURejectsInputBeforeHTTP(t *testing.T) {
	var calls atomic.Int64
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1) }))
	defer srv.Close()
	m := NewMinerUModel(map[string]string{"default": srv.URL}, URLSuffix{DocumentParse: "task"})
	key, name, blank := "key", "vlm", " "
	api := &APIConfig{ApiKey: &key}
	for _, input := range []struct {
		name    *string
		content []byte
		url     string
		api     *APIConfig
	}{
		{&name, []byte("local file"), "", api},
		{&name, nil, "file:///private", api},
		{&name, nil, "https://files.invalid/doc.pdf", nil},
		{&name, nil, "https://files.invalid/doc.pdf", &APIConfig{ApiKey: &blank}},
	} {
		if _, err := m.ParseFile(input.name, input.content, stringPtr(input.url), input.api, nil); err == nil {
			t.Fatal("invalid submission accepted")
		}
	}
	if _, err := m.ShowTask("../secret", api); err == nil {
		t.Fatal("unsafe task ID accepted")
	}
	if calls.Load() != 0 {
		t.Fatal("invalid request reached provider")
	}
}

func TestMinerUCancellationAndHTTPFailure(t *testing.T) {
	key, name := "private-key", "vlm"
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/wait/job-1" {
			<-r.Context().Done()
			return
		}
		w.WriteHeader(502)
		fmt.Fprint(w, "private-provider-body-"+key)
	}))
	defer srv.Close()
	m := NewMinerUModel(map[string]string{"default": srv.URL}, URLSuffix{DocumentParse: "fail"})
	if _, err := m.ParseFile(&name, nil, stringPtr("https://files.invalid/doc.pdf"), &APIConfig{ApiKey: &key}, nil); err == nil || strings.Contains(err.Error(), key) || strings.Contains(err.Error(), "private-provider-body") {
		t.Fatal("HTTP failure lost or leaked", err)
	}
	m.baseModel.URLSuffix.DocumentParse = "wait"
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Millisecond)
	defer cancel()
	if _, err := m.ShowTask("job-1", &APIConfig{ApiKey: &key, Context: ctx}); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("deadline lost", err)
	}
}

func TestMinerUOptionalModelAndURLPriority(t *testing.T) {
	calls := 0
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if r.URL.Path != "/api/task" || body["url"] != "https://files.invalid/doc.pdf" || body["model_version"] != nil {
			t.Error("optional URL parser payload", body, r.URL)
		}
		fmt.Fprint(w, `{"code":0,"data":{"task_id":"job"}}`)
	}))
	m := NewMinerUModel(map[string]string{"default": srv.URL}, URLSuffix{DocumentParse: "task"})
	for _, name := range []*string{nil, stringPtr("")} {
		result, err := m.ParseFile(name, []byte("ignored URL-only input"), stringPtr("https://files.invalid/doc.pdf"), &APIConfig{ApiKey: stringPtr("key")}, nil)
		if err != nil || result == nil || result.TaskID != "job" {
			t.Fatal(result, err)
		}
	}
	if calls != 2 {
		t.Fatal(calls)
	}
}
