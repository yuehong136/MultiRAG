package cli

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"sync/atomic"
	"testing"
)

func TestDatasetSearchRESTContract(t *testing.T) {
	for _, mode := range []string{"normal", "native", "concurrent"} {
		t.Run(mode, func(t *testing.T) {
			var searches atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer owned-session" {
					t.Error("missing Bearer session")
				}
				switch r.URL.Path {
				case "/api/v1/datasets":
					fmt.Fprint(w, `{"code":0,"data":[{"id":"a-id","name":"a"},{"id":"b-id","name":"b"}]}`)
				case "/api/v1/datasets/search":
					searches.Add(1)
					var body struct {
						DatasetIDs []string `json:"dataset_ids"`
						KBID       any      `json:"kb_id"`
						Question   string   `json:"question"`
					}
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
						return
					}
					if r.Method != http.MethodPost || len(body.DatasetIDs) != 2 || body.DatasetIDs[0] != "a-id" || body.DatasetIDs[1] != "b-id" || body.KBID != nil || body.Question != "question" {
						t.Errorf("wrong search payload: %+v", body)
					}
					fmt.Fprint(w, `{"code":0,"data":{"chunks":[{"chunk_id":"chunk","doc_id":"doc","kb_id":"a-id","content_with_weight":"answer"}]}}`)
				default:
					t.Errorf("obsolete or unknown path: %s", r.URL.Path)
					http.NotFound(w, r)
				}
			}))
			defer server.Close()
			address, _ := url.Parse(server.URL)
			port, _ := strconv.Atoi(address.Port())
			client := NewMultiRAGClient("user")
			client.HTTPClient.Host = address.Hostname()
			client.HTTPClient.Port = port
			client.HTTPClient.LoginToken = "owned-session"
			cmd := NewCommand("search_on_datasets")
			cmd.Params["datasets"] = "a,b"
			cmd.Params["question"] = "question"
			switch mode {
			case "normal":
				result, err := client.ExecuteCommand(cmd)
				if err != nil {
					t.Fatal(err)
				}
				rows := result.(*CommonResponse).Data
				if len(rows) != 1 || rows[0]["id"] != "chunk" || rows[0]["content"] != "answer" {
					t.Fatal(rows)
				}
			case "native":
				cmd.Params["iterations"] = 3
				result, err := client.ExecuteCommand(cmd)
				if err != nil {
					t.Fatal(err)
				}
				raw := result.(*BenchmarkRawResponse)
				if len(raw.ResponseList) != 3 {
					t.Fatal(raw)
				}
				for _, resp := range raw.ResponseList {
					if !isSuccess(resp, cmd.Type) {
						t.Fatal("benchmark rejected successful response")
					}
				}
			case "concurrent":
				benchmark := NewCommand("benchmark")
				benchmark.Params["concurrency"] = 2
				benchmark.Params["iterations"] = 3
				benchmark.Params["command"] = cmd
				if _, err := client.ExecuteCommand(benchmark); err != nil {
					t.Fatal(err)
				}
			}
			want := map[string]int32{"normal": 1, "native": 3, "concurrent": 6}[mode]
			if searches.Load() != want {
				t.Fatalf("got %d searches, want %d", searches.Load(), want)
			}
		})
	}
}

func TestDatasetSearchRejectsBusinessFailure(t *testing.T) {
	for _, body := range []string{`{"code":403,"message":"denied"}`, `{}`, `{"code":"0"}`, `not json`} {
		t.Run(body, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/api/v1/datasets" {
					fmt.Fprint(w, `{"code":0,"data":[{"id":"a-id","name":"a"}]}`)
					return
				}
				fmt.Fprint(w, body)
			}))
			defer server.Close()
			address, _ := url.Parse(server.URL)
			port, _ := strconv.Atoi(address.Port())
			client := NewMultiRAGClient("user")
			client.HTTPClient.Host = address.Hostname()
			client.HTTPClient.Port = port
			cmd := NewCommand("search_on_datasets")
			cmd.Params["datasets"] = "a"
			cmd.Params["question"] = "question"
			if _, err := client.ExecuteCommand(cmd); err == nil {
				t.Fatal("business failure accepted")
			}
			cmd.Params["iterations"] = 2
			result, err := client.ExecuteCommand(cmd)
			if err != nil {
				t.Fatal(err)
			}
			for _, resp := range result.(*BenchmarkRawResponse).ResponseList {
				if isSuccess(resp, cmd.Type) {
					t.Fatal("benchmark counted failed response as success")
				}
			}
		})
	}
}
