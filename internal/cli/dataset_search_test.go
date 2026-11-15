package cli

import (
	"encoding/json"
	"fmt"
	"io"
	"multirag/tests/support"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
)

func TestDatasetSearchRESTContract(t *testing.T) {
	for _, mode := range []string{"normal", "native", "concurrent"} {
		t.Run(mode, func(t *testing.T) {
			var searches atomic.Int32
			server := support.RegisteredHTTPServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "owned-session" {
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
			client := newConsumerCLI("user")
			consumerHTTP(client).Host = address.Hostname()
			consumerHTTP(client).Port = port
			consumerHTTP(client).LoginToken = consumerTokenPtr("owned-session")
			cmd := NewCommand("search_on_datasets")
			cmd.Params["datasets"] = "a,b"
			cmd.Params["question"] = "question"
			switch mode {
			case "normal":
				result, err := client.ExecuteCommand(cmd)
				if err != nil {
					t.Fatal(err)
				}
				if result != nil {
					t.Fatal("direct-print search returned a facade", result)
				}
			case "native":
				cmd.Params["iterations"] = 3
				result, err := client.ExecuteCommand(cmd)
				if err != nil {
					t.Fatal(err)
				}
				raw := result.(*BenchmarkResponse)
				if raw.SuccessCount != 3 || raw.FailureCount != 0 {
					t.Fatal(raw)
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
			server := support.RegisteredHTTPServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/api/v1/datasets" {
					fmt.Fprint(w, `{"code":0,"data":[{"id":"a-id","name":"a"}]}`)
					return
				}
				fmt.Fprint(w, body)
			}))
			defer server.Close()
			address, _ := url.Parse(server.URL)
			port, _ := strconv.Atoi(address.Port())
			client := newConsumerCLI("user")
			consumerHTTP(client).Host = address.Hostname()
			consumerHTTP(client).Port = port
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
			if result.(*BenchmarkResponse).SuccessCount != 0 || result.(*BenchmarkResponse).FailureCount != 2 {
				t.Fatal("benchmark counted failed response as success")
			}
		})
	}
}

func TestDatasetSearchSingleIterationBenchmark(t *testing.T) {
	for _, test := range []struct{ body, want string }{
		{`{"code":0,"data":{"chunks":[]}}`, "COMMAND_COUNT: 1, SUCCESS: 1, FAILURE: 0"},
		{`{"code":403,"message":"denied"}`, "COMMAND_COUNT: 1, SUCCESS: 0, FAILURE: 1"},
	} {
		t.Run(test.want, func(t *testing.T) {
			server := support.RegisteredHTTPServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/api/v1/datasets" {
					fmt.Fprint(w, `{"code":0,"data":[{"id":"a-id","name":"a"}]}`)
					return
				}
				if r.URL.Path != "/api/v1/datasets/search" {
					t.Error("obsolete search path")
				}
				fmt.Fprint(w, test.body)
			}))
			defer server.Close()
			address, _ := url.Parse(server.URL)
			port, _ := strconv.Atoi(address.Port())
			client := newConsumerCLI("user")
			consumerHTTP(client).Host = address.Hostname()
			consumerHTTP(client).Port = port
			nested := NewCommand("search_on_datasets")
			nested.Params["datasets"] = "a"
			nested.Params["question"] = "question"
			benchmark := NewCommand("benchmark")
			benchmark.Params["command"] = nested
			benchmark.Params["concurrency"] = 1
			benchmark.Params["iterations"] = 1
			reader, writer, err := os.Pipe()
			if err != nil {
				t.Fatal(err)
			}
			old := os.Stdout
			os.Stdout = writer
			defer func() { os.Stdout = old }()
			result, runErr := client.RunBenchmark(benchmark)
			writer.Close()
			os.Stdout = old
			output, readErr := io.ReadAll(reader)
			reader.Close()
			_ = output
			if readErr != nil {
				t.Fatal(readErr)
			}
			stats, ok := result.(*BenchmarkResponse)
			if !ok || stats.SuccessCount+stats.FailureCount != 1 {
				t.Fatal("single iteration accounting", result, runErr)
			}
			failed := strings.Contains(test.want, "SUCCESS: 0")
			if (runErr != nil) != failed || stats.SuccessCount != map[bool]int{true: 0, false: 1}[failed] {
				t.Fatal("single iteration false success", stats, runErr)
			}
		})
	}
}
