package handler_test

import (
	"encoding/json"
	"fmt"
	"io"
	"multirag/internal/cli"
	"multirag/internal/testsupport"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
)

func TestAfter410GoHTTPCLIAndSQL(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	t.Run("vllm-rerank", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) { exerciseCanonicalInference(t, f, "vllm", "ranker", instance, "rerank") })
		}
	})
	t.Run("speech-providers", func(t *testing.T) {
		for _, provider := range []string{"SiliconFlow", "StepFun"} {
			for _, instance := range []string{"one", "two"} {
				t.Run(provider+"/"+instance, func(t *testing.T) { exerciseCanonicalMedia(t, f, provider, "owned-tts", instance, "tts") })
			}
		}
		exerciseCanonicalMedia(t, f, "SiliconFlow", "owned-asr", "one", "speech2text")
	})
	t.Run("ocr-and-cohere", func(t *testing.T) {
		for _, provider := range []string{"Baidu", "Mistral", "PaddleOCR"} {
			for _, instance := range []string{"one", "two"} {
				t.Run(provider+"/"+instance, func(t *testing.T) { exerciseCanonicalMedia(t, f, provider, "owned-ocr", instance, "ocr") })
			}
		}
		for _, instance := range []string{"one", "two"} {
			t.Run("CoHere/"+instance, func(t *testing.T) {
				exerciseCanonicalMedia(t, f, "CoHere", "cohere-transcribe-03-2026", instance, "speech2text")
			})
		}
	})
	t.Run("parse", func(t *testing.T) {
		for _, instance := range []string{"one", "two"} {
			t.Run(instance, func(t *testing.T) {
				var calls atomic.Int64
				var mode atomic.Int32
				key := "owned-gitee-" + instance
				upstream := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					calls.Add(1)
					if r.Header.Get("Authorization") != "Bearer "+key {
						t.Error("Gitee task key crossed instance")
					}
					switch {
					case r.Method == "POST" && strings.HasSuffix(r.URL.Path, "/async/documents/parse"):
						if err := r.ParseMultipartForm(1 << 20); err != nil {
							t.Error(err)
							return
						}
						defer r.MultipartForm.RemoveAll()
						if r.FormValue("model") != "MinerU2.5" {
							t.Error("Gitee native model lost")
						}
						if file, _, err := r.FormFile("file"); err == nil {
							raw, _ := io.ReadAll(file)
							file.Close()
							if string(raw) != "owned document" {
								t.Error("Gitee parse file changed")
							}
						} else if r.FormValue("file") != "https://example.invalid/doc.pdf" {
							t.Error("Gitee parse URL changed")
						}
						fmt.Fprint(w, `{"task_id":"owned-task","status":"pending"}`)
					case r.Method == "GET" && strings.HasSuffix(r.URL.Path, "/tasks"):
						fmt.Fprint(w, `{"items":[{"task_id":"owned-task","status":"pending"}]}`)
					case r.Method == "GET" && strings.HasSuffix(r.URL.Path, "/task/owned-task/get"):
						if mode.Load() == 1 {
							fmt.Fprint(w, `{"error":"private-task-failure"}`)
							return
						}
						fmt.Fprint(w, `{"segments":[{"index":1,"content":"page two"},{"index":0,"content":"page one"}]}`)
					default:
						t.Error("native Gitee task endpoint changed", r.Method, r.URL.Path)
					}
				}))
				id := f.enroll(t, "Gitee", instance, key, upstream.URL, "MinerU2.5", "doc_parse")
				before := f.snapshot(t)
				body := map[string]any{"model_id": id, "content": []byte("owned document")}
				data := f.requireSuccess(t, "owner", "POST", "/api/v1/file/parse", body)["data"].(map[string]any)
				if data["task_id"] != "owned-task" || len(data) != 1 {
					t.Fatal("native async submission fabricated parsed output")
				}
				f.requireSuccess(t, "owner", "POST", "/api/v1/file/parse", map[string]any{"model_id": id, "url": "https://example.invalid/doc.pdf"})
				route := "/api/v1/providers/Gitee/instances/" + instance + "/tasks"
				f.requireSuccess(t, "owner", "GET", route, nil)
				result := f.requireSuccess(t, "owner", "GET", route+"/owned-task", nil)["data"].(map[string]any)
				segments := result["segments"].([]any)
				if len(segments) != 2 || segments[0].(map[string]any)["index"] != float64(0) || segments[1].(map[string]any)["content"] != "page two" {
					t.Fatal("native task segment ordering/pages lost", result)
				}
				for _, syntax := range []string{fmt.Sprintf("PARSE WITH '%s' URL 'https://example.invalid/doc.pdf';", id), fmt.Sprintf("LIST '%s@Gitee' TASKS;", instance), fmt.Sprintf("SHOW '%s@Gitee' TASK 'owned-task';", instance)} {
					command, err := cli.NewParser(syntax).Parse(cli.APIMode)
					if err != nil {
						t.Fatal(err)
					}
					response, err := f.cli(t, "owner").ExecuteCommand(command)
					if err != nil {
						t.Fatal("Gitee actual CLI HTTP", err)
					}
					encoded, _ := json.Marshal(response)
					if !strings.Contains(string(encoded), "owned-task") && !strings.Contains(string(encoded), "page two") {
						t.Fatal("CLI task result lost", string(encoded))
					}
				}
				count := calls.Load()
				for _, who := range []string{"", "foreign", "inactive", "revoked", "invited", "member", "disabledtenant"} {
					for _, operation := range []struct {
						method, path string
						body         any
					}{{"POST", "/api/v1/file/parse", body}, {"GET", route, nil}, {"GET", route + "/owned-task", nil}} {
						_, failed, _ := f.request(t, who, operation.method, operation.path, operation.body)
						if providerEnvelopeError(failed) == nil {
							t.Fatal("denied parse/task accepted", who)
						}
					}
				}
				if calls.Load() != count || f.snapshot(t) != before {
					t.Fatal("denied parse/task affected supplier or SQL")
				}
				mode.Store(1)
				_, failed, raw := f.request(t, "owner", "GET", route+"/owned-task", nil)
				if providerEnvelopeError(failed) == nil || strings.Contains(raw, "private-task-failure") || f.snapshot(t) != before {
					t.Fatal("task failure accepted, leaked or wrote SQL")
				}
			})
		}
	})
}
