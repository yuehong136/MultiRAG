package models

import (
	"context"
	"fmt"
	"io"
	"net/http"

	"strings"
	"testing"
	"time"
)

func TestGiteeParseTaskContract(t *testing.T) {
	key, model := "controlled-parse-key", "MinerU2.5"
	status, submit, output := "pending", `{"task_id":"job-1","status":"pending"}`, `{"segments":[{"index":1,"content":"page one"},{"index":2,"content":"page two"}]}`
	calls := 0
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Header.Get("Authorization") != "Bearer "+key {
			t.Error("missing provider key")
		}
		switch r.URL.Path {
		case "/parse":
			if err := r.ParseMultipartForm(modelResponseLimitForTest); err != nil {
				t.Error(err)
				return
			}
			defer r.MultipartForm.RemoveAll()
			if r.FormValue("model") != model {
				t.Error("model missing")
			}
			if r.FormValue("file") == "" {
				file, _, err := r.FormFile("file")
				if err != nil {
					t.Error(err)
					return
				}
				defer file.Close()
				b, _ := io.ReadAll(file)
				if string(b) != "document bytes" {
					t.Error("binary input lost")
				}
			}
			fmt.Fprint(w, submit)
		case "/tasks":
			fmt.Fprintf(w, `{"items":[{"task_id":"job-1","status":%q}]}`, status)
		case "/task/job-1/status":
			fmt.Fprintf(w, `{"task_id":"job-1","status":%q}`, status)
		case "/task/job-1/get":
			fmt.Fprint(w, output)
		default:
			t.Error("wrong task path", r.URL.Path)
			w.WriteHeader(404)
		}
	}))
	defer srv.Close()
	driver := NewGiteeModel(map[string]string{"default": srv.URL}, URLSuffix{DocumentParse: "parse", Tasks: "tasks", Task: "task"})
	api := &APIConfig{ApiKey: &key}
	for _, input := range []struct {
		content []byte
		url     string
	}{{[]byte("document bytes"), ""}, {nil, "https://files.invalid/doc.pdf"}} {
		var remote *string
		if input.url != "" {
			remote = stringPtr(input.url)
		}
		r, err := driver.ParseFile(&model, input.content, remote, api, nil)
		if err != nil || r.TaskID != "job-1" {
			t.Fatal(r, err)
		}
	}
	if tasks, err := driver.ListTasks(api); err != nil || len(tasks) != 1 {
		t.Fatal(tasks, err)
	}
	if r, err := driver.ShowTask("job-1", api); err != nil || len(r.Segments) != 2 {
		t.Fatal(r, err)
	}
	status = "success"
	if r, err := driver.ShowTask("job-1", api); err != nil || len(r.Segments) != 2 {
		t.Fatal(r, err)
	}
	for _, bad := range []string{`{}`, `{"status":"pending"}`, `{"task_id":"job-1","status":"failed"}`, `{"error":"private-provider-body"}`} {
		submit = bad
		if _, err := driver.ParseFile(&model, []byte("document bytes"), nil, api, nil); err == nil || strings.Contains(err.Error(), "private-provider-body") {
			t.Fatal("bad submit accepted/leaked", err)
		}
	}

	status = "success"
	for _, bad := range []string{`{}`, `{"segments":[]}`, `{"segments":[{"index":1,"content":"one"},{"index":1,"content":"two"}]}`, `{"segments":[{"index":1,"content":""}]}`, `{"segments":[{"content":"one"}]}`, `{"segments":[{"index":null,"content":"one"}]}`, `{"segments":[{"index":0,"content":null}]}`} {
		output = bad
		if _, err := driver.ShowTask("job-1", api); err == nil {
			t.Fatal("invalid result accepted")
		}
	}
	output = `{"segments":[{"index":2,"content":"two"},{"index":0,"content":"zero"},{"index":1,"content":"one"}]}`
	if result, err := driver.ShowTask("job-1", api); err != nil || len(result.Segments) != 3 || result.Segments[0].Index != 0 || result.Segments[1].Index != 1 || result.Segments[2].Index != 2 {
		t.Fatal("legal zero or stable segment order lost", result, err)
	}
	before := calls
	if _, err := driver.ShowTask("../secret", api); err == nil {
		t.Fatal("unsafe task ID accepted")
	}

	if before != calls {
		t.Fatal("invalid input called provider")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := driver.ListTasks(&APIConfig{ApiKey: &key, Context: ctx}); err == nil {
		t.Fatal("cancelled request succeeded")
	}
}

func TestGiteeTaskDeadlineAndHTTPFailure(t *testing.T) {
	key := "controlled-key"
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/wait" {
			io.Copy(io.Discard, r.Body)
			<-r.Context().Done()
			return
		}
		w.WriteHeader(502)
		fmt.Fprint(w, "private-body-"+key)
	}))
	defer srv.Close()
	d := NewGiteeModel(map[string]string{"default": srv.URL}, URLSuffix{Tasks: "failed"})
	if _, err := d.ListTasks(&APIConfig{ApiKey: &key}); err == nil || strings.Contains(err.Error(), key) || strings.Contains(err.Error(), "private-body") {
		t.Fatal("HTTP failure lost/leaked", err)
	}
	d.baseModel.URLSuffix.Tasks = "wait"
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	if _, err := d.ListTasks(&APIConfig{ApiKey: &key, Context: ctx}); err == nil {
		t.Fatal("deadline ignored")
	}
}
