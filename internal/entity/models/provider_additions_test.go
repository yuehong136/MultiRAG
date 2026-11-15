package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
)

func addedProviderServer(t *testing.T, handler http.HandlerFunc) *httptest.Server {
	t.Helper()
	return providerTestServer(t, handler)
}

func addedProviderDriver(t *testing.T, name, endpoint string) ModelDriver {
	t.Helper()
	driver, err := NewModelFactory().CreateModelDriver(name, map[string]string{"default": endpoint}, URLSuffix{Chat: "chat", Models: "models", Embedding: "embed", Rerank: "rerank", Balance: "credit"})
	if err != nil || driver.Name() != strings.ToLower(name) {
		t.Fatalf("factory %s: %T %v", name, driver, err)
	}
	return driver
}

func addedProviderAPI() *APIConfig {
	key := "fixture-key"
	return &APIConfig{ApiKey: &key}
}

func addedProviderName(name string) *string { return &name }

func TestAddedProvidersChatAndStreaming(t *testing.T) {
	for _, name := range []string{"StepFun", "Baichuan", "Upstage", "CoHere"} {
		t.Run(name, func(t *testing.T) {
			var calls atomic.Int32
			server := addedProviderServer(t, func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.URL.Path != "/chat" || r.Method != http.MethodPost || (!strings.HasPrefix(r.Header.Get("Authorization"), "Bearer ") && !strings.HasPrefix(r.Header.Get("Authorization"), "bearer ")) || strings.TrimPrefix(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "), "bearer ") != "fixture-key" {
					t.Error("wrong request", r.Method, r.URL.Path)
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				messages := body["messages"].([]any)
				if len(messages) != 3 || messages[0].(map[string]any)["role"] != "system" || messages[1].(map[string]any)["role"] != "assistant" {
					t.Error("history lost", messages)
				}
				if body["max_tokens"] != float64(128) || body["temperature"] != float64(0) {
					t.Error("configuration lost", body)
				}
				if name == "CoHere" {
					wantP := any(nil)
					if body["stream"] == true {
						wantP = float64(0.8)
					}
					if body["top_p"] != 0.8 || body["p"] != wantP || body["thinking"].(map[string]any)["type"] != "enabled" {
						t.Error("Cohere body", body)
					}
				} else if body["top_p"] != 0.8 {
					t.Error("top_p lost", body)
				}
				if name == "Upstage" && body["reasoning_effort"] != "high" {
					t.Error("effort lost", body)
				}
				if body["stream"] == true {
					if name == "CoHere" {
						fmt.Fprint(w, "event: message-start\ndata: {\"type\":\"message-start\"}\n\ndata: {\"type\":\"content-delta\",\"delta\":{\"message\":{\"content\":{\"thinking\":\"trace\",\"text\":\"answer\"}}}}\n\ndata: {\"type\":\"message-end\",\"delta\":{\"finish_reason\":\"COMPLETE\"}}\n\n")
					} else {
						field := "reasoning_content"
						if name == "Upstage" {
							field = "reasoning"
						}
						fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"%s\":\"trace\",\"content\":\"answer\"},\"finish_reason\":\"stop\"}]}\n\n", field)
					}
				} else if name == "CoHere" {
					fmt.Fprint(w, `{"message":{"content":[{"type":"thinking","thinking":"trace"},{"type":"text","text":"answer"}]}}`)
				} else {
					field := "reasoning_content"
					if name == "Upstage" {
						field = "reasoning"
					}
					fmt.Fprintf(w, `{"choices":[{"message":{"content":"answer",%q:"trace"}}]}`, field)
				}
			})
			driver := addedProviderDriver(t, name, server.URL)
			maxTokens, temperature, topP, thinking, stream, effort := 128, 0.0, 0.8, true, true, "high"
			config := &ChatConfig{MaxTokens: &maxTokens, Temperature: &temperature, TopP: &topP, Thinking: &thinking, Stream: nil, Effort: &effort}
			messages := []Message{{Role: "system", Content: "rules"}, {Role: "assistant", Content: "prior"}, {Role: "user", Content: "question"}}
			expectedReason := "trace"
			if name == "StepFun" || name == "Baichuan" {
				expectedReason = ""
			}
			response, err := driver.ChatWithMessages("model", messages, addedProviderAPI(), config)
			if err != nil || response == nil || response.Answer == nil || *response.Answer != "answer" || response.ReasonContent == nil || *response.ReasonContent != expectedReason {
				t.Fatalf("chat: %#v %v", response, err)
			}
			stream = true
			config.Stream = &stream
			var events []string
			err = driver.ChatStreamlyWithSender("model", messages, addedProviderAPI(), config, func(answer, reasoning *string) error {
				if reasoning != nil {
					events = append(events, "r:"+*reasoning)
				}
				if answer != nil {
					events = append(events, "a:"+*answer)
				}
				return nil
			})
			expectedEvents := []string{"r:trace", "a:answer", "a:[DONE]"}
			if name == "StepFun" || name == "Baichuan" {
				expectedEvents = expectedEvents[1:]
			}
			if err != nil || !reflect.DeepEqual(events, expectedEvents) || calls.Load() != 2 {
				t.Fatal(events, err, calls.Load())
			}
		})
	}
}

func TestAddedProvidersStreamFailureAndCancellation(t *testing.T) {
	for _, name := range []string{"StepFun", "Baichuan", "Upstage", "CoHere"} {
		for _, mode := range []string{"malformed", "truncated", "provider-error", "empty", "sender-error", "cancel"} {
			t.Run(name+"/"+mode, func(t *testing.T) {
				server := addedProviderServer(t, func(w http.ResponseWriter, r *http.Request) {
					if mode == "malformed" {
						fmt.Fprint(w, "data: bad-json\n\n")
						return
					}
					if mode == "provider-error" {
						fmt.Fprint(w, "data: {\"error\":{\"message\":\"failure\"}}\n\n")
						return
					}
					if mode == "empty" {
						if name == "CoHere" {
							fmt.Fprint(w, "data: {\"type\":\"message-end\"}\n\n")
						} else {
							fmt.Fprint(w, "data: [DONE]\n\n")
						}
						return
					}
					if name == "CoHere" {
						fmt.Fprint(w, "data: {\"type\":\"content-delta\",\"delta\":{\"message\":{\"content\":{\"text\":\"answer\"}}}}\n\n")
					} else {
						fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\n")
					}
					w.(http.Flusher).Flush()
					if mode == "cancel" {
						<-r.Context().Done()
					}
				})
				driver := addedProviderDriver(t, name, server.URL)
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				api := addedProviderAPI()
				api.Context = ctx
				marker := errors.New("sender failure")
				done := false
				err := driver.ChatStreamlyWithSender("model", []Message{{Role: "user", Content: "q"}}, api, nil, func(answer, reasoning *string) error {
					if answer != nil && *answer == "[DONE]" {
						done = true
					}
					if mode == "sender-error" {
						return marker
					}
					if mode == "cancel" {
						cancel()
					}
					return nil
				})
				if err == nil || done {
					t.Fatalf("false completion: %v done=%v", err, done)
				}
				if mode == "sender-error" && !errors.Is(err, marker) {
					t.Fatal(err)
				}
				if mode == "cancel" && !errors.Is(err, context.Canceled) {
					t.Fatal(err)
				}
			})
		}
	}
}

func TestAddedProviderEmbeddingsBatchesAndQuery(t *testing.T) {
	for _, provider := range []string{"CoHere", "Baichuan", "Upstage"} {
		for _, query := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/query=%t", provider, query), func(t *testing.T) {
				modelName := "solar-embedding-1-large-passage"
				if provider == "Upstage" && query {
					modelName = "solar-embedding-1-large-query"
				}
				var calls atomic.Int32
				server := addedProviderServer(t, func(w http.ResponseWriter, r *http.Request) {
					calls.Add(1)
					var body map[string]any
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
					}
					inputField, dimensionField := "input", "dimensions"
					if provider == "CoHere" {
						inputField, dimensionField = "texts", "output_dimension"
						expected := "search_document"

						if body["input_type"] != expected {
							t.Error(body)
						}
					}
					if provider == "Upstage" || provider == "Baichuan" {
						if body[dimensionField] != nil {
							t.Error("unsupported Upstage dimension wire field added")
						}
					} else if body[dimensionField] != float64(2) {
						t.Error("dimension lost", body)
					}
					if provider == "Upstage" {
						expected := "solar-embedding-1-large-passage"
						if query {
							expected = "solar-embedding-1-large-query"
						}
						if body["model"] != expected {
							t.Error(body)
						}
					}
					inputs := body[inputField].([]any)
					if len(inputs) != 65 {
						t.Error("native single request cardinality", len(inputs))
					}
					if provider == "CoHere" {
						vectors := make([][]float64, len(inputs))
						for i, input := range inputs {
							value, _ := strconv.Atoi(input.(string))
							vectors[i] = []float64{float64(value), 0}
						}
						json.NewEncoder(w).Encode(map[string]any{"embeddings": map[string]any{"float": vectors}})
					} else {
						data := make([]map[string]any, len(inputs))
						for i, input := range inputs {
							value, _ := strconv.Atoi(input.(string))
							data[len(inputs)-1-i] = map[string]any{"index": i, "embedding": []float64{float64(value), 0}}
						}
						json.NewEncoder(w).Encode(map[string]any{"data": data})
					}
				})
				driver := addedProviderDriver(t, provider, server.URL)
				texts := make([]string, 65)
				for i := range texts {
					texts[i] = strconv.Itoa(i)
				}
				name := modelName
				config := &EmbeddingConfig{Dimension: 2}
				data, err := driver.Embed(&name, texts, addedProviderAPI(), config)
				if err != nil || len(data) != 65 || calls.Load() != 1 {
					t.Fatal(len(data), err, calls.Load())
				}
				seen := map[int]bool{}
				for _, item := range data {
					if item.Index < 0 || item.Index >= len(texts) || seen[item.Index] || !reflect.DeepEqual(item.Embedding, []float64{float64(item.Index), 0}) {
						t.Fatal("native typed index/value lost", item)
					}
					seen[item.Index] = true
				}
				if config.Dimension != 2 {
					t.Fatal("mutated caller inputs")
				}
			})
		}
	}
}

func TestAddedProviderEmbeddingFailures(t *testing.T) {
	for _, provider := range []string{"CoHere", "Baichuan", "Upstage"} {
		cases := []string{`{}`, `{"error":{}}`, `{"data":[{"index":0,"embedding":[null]}],"embeddings":{"float":[[null]]}}`, `{"data":[{"index":0,"embedding":[]}],"embeddings":{"float":[[]]}}`, `{"data":[{"index":0,"embedding":[1,2]}],"embeddings":{"float":[[1,2]]}}`, `{"data":[{"index":0,"embedding":[1e999]}],"embeddings":{"float":[[1e999]]}}`, `{"data":[{"index":null,"embedding":[0]}]}`, `{"data":[{"index":-1,"embedding":[0]}]}`, `{"data":[{"index":3,"embedding":[0]}]}`, `{"data":[{"index":0,"embedding":[0]}],"embeddings":{"float":[[0]]}} {}`}
		for i, payload := range cases {
			t.Run(fmt.Sprintf("%s/%d", provider, i), func(t *testing.T) {
				server := addedProviderServer(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, payload) })
				driver := addedProviderDriver(t, provider, server.URL)
				data, err := driver.Embed(addedProviderName("model"), []string{"a"}, addedProviderAPI(), &EmbeddingConfig{Dimension: 1})
				if err == nil || data != nil {
					t.Fatal("malformed embeddings accepted", data, err)
				}
			})
		}
		t.Run(provider+"/duplicate", func(t *testing.T) {
			server := addedProviderServer(t, func(w http.ResponseWriter, r *http.Request) {
				fmt.Fprint(w, `{"data":[{"index":0,"embedding":[0]},{"index":0,"embedding":[0]}],"embeddings":{"float":[[0]]}}`)
			})
			data, err := addedProviderDriver(t, provider, server.URL).Embed(addedProviderName("model"), []string{"a", "b"}, addedProviderAPI(), nil)
			if err == nil || data != nil {
				t.Fatal(data, err)
			}
		})
	}
}

func TestAddedProviderValidationAndIsolation(t *testing.T) {
	for _, provider := range []string{"CoHere", "FishAudio", "StepFun", "Baichuan", "Upstage"} {
		t.Run(provider, func(t *testing.T) {
			var calls atomic.Int32
			server := addedProviderServer(t, func(w http.ResponseWriter, r *http.Request) { calls.Add(1); http.Error(w, "fixture", 500) })
			urls := map[string]string{"default": server.URL}
			driver, err := NewModelFactory().CreateModelDriver(provider, urls, URLSuffix{Chat: "chat", Models: "models", Embedding: "embed", Balance: "credit"})
			if err != nil {
				t.Fatal(err)
			}
			urls["default"] = "http://127.0.0.1:1"
			if _, err := driver.ListModels(addedProviderAPI()); err == nil {
				t.Fatal("HTTP failure accepted")
			}
			if provider != "Baichuan" && calls.Load() != 1 {
				t.Fatal("base URL not cloned", calls.Load())
			}
			calls.Store(0)
			for _, api := range []*APIConfig{nil, {}, {ApiKey: addedProviderName(" ")}, {ApiKey: addedProviderName("fixture-key"), Region: addedProviderName("unknown")}} {
				if _, err := driver.ListModels(api); err == nil {
					t.Fatal("invalid configuration accepted")
				}
				if _, err := driver.ChatWithMessages("model", []Message{{Role: "user", Content: "q"}}, api, nil); err == nil {
					t.Fatal("invalid chat configuration accepted")
				}
			}
			if calls.Load() != 0 {
				t.Fatal("invalid config reached provider", calls.Load())
			}
			if provider == "CoHere" || provider == "Baichuan" || provider == "Upstage" {
				data, err := driver.Embed(nil, nil, addedProviderAPI(), nil)
				if err != nil || data == nil || len(data) != 0 {
					t.Fatal(data, err)
				}
				for _, config := range []*EmbeddingConfig{nil, {Dimension: -1}} {
					if _, err := driver.Embed(nil, []string{"a"}, addedProviderAPI(), config); err == nil {
						t.Fatal("missing model accepted")
					}
				}
			}
		})
	}
}

func TestCoHereRerankStrictContract(t *testing.T) {
	for _, config := range []*RerankConfig{nil, {}, {TopN: 1}, {TopN: 9}} {
		t.Run(fmt.Sprint(config), func(t *testing.T) {
			server := addedProviderServer(t, func(w http.ResponseWriter, r *http.Request) {
				var body map[string]any
				json.NewDecoder(r.Body).Decode(&body)
				top := 2
				if config != nil && config.TopN != 0 {
					top = config.TopN
				}
				if body["top_n"] != float64(top) || body["query"] != "q" {
					t.Error(body)
				}
				results := []map[string]any{{"index": 1, "relevance_score": 0}}
				if top >= 2 {
					results = append(results, map[string]any{"index": 0, "relevance_score": 0.5})
				}
				json.NewEncoder(w).Encode(map[string]any{"results": results})
			})
			response, err := addedProviderDriver(t, "CoHere", server.URL).Rerank(addedProviderName("model"), "q", []string{"a", "b"}, addedProviderAPI(), config)
			if err != nil || response == nil {
				t.Fatal(response, err)
			}
			top, _ := expectedRerankTopNForTest(2, config)
			if len(response.Data) != top {
				t.Fatal("rerank cardinality", response)
			}
		})
	}
	for i, payload := range []string{`{}`, `{"results":[{"index":0}]}`, `{"results":[{"index":0,"relevance_score":null}]}`, `{"results":[{"index":0,"relevance_score":0},{"index":0,"relevance_score":1}]}`, `{"results":[{"index":2,"relevance_score":0}]}`, `{"results":[{"index":0,"relevance_score":1e999}]}`} {
		t.Run(fmt.Sprint(i), func(t *testing.T) {
			server := addedProviderServer(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, payload) })
			response, err := addedProviderDriver(t, "CoHere", server.URL).Rerank(addedProviderName("model"), "q", []string{"a"}, addedProviderAPI(), nil)
			if err == nil || response != nil {
				t.Fatal(response, err)
			}
		})
	}
}

func TestAddedProviderDiscoveryAndBalance(t *testing.T) {
	for _, provider := range []string{"CoHere", "FishAudio", "StepFun", "Upstage"} {
		t.Run(provider, func(t *testing.T) {
			var calls atomic.Int32
			server := addedProviderServer(t, func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				switch provider {
				case "CoHere":
					if r.URL.Query().Get("page_token") == "" {
						fmt.Fprint(w, `{"models":[{"name":"first"}],"next_page_token":"next page"}`)
					} else {
						if r.URL.Query().Get("page_token") != "next page" {
							t.Error(r.URL)
						}
						fmt.Fprint(w, `{"models":[{"name":"second"}]}`)
					}
				case "FishAudio":
					if r.URL.Path == "/wallet/self/api-credit" {
						fmt.Fprint(w, `{"credit":0,"user_id":"fixture"}`)
					} else {
						fmt.Fprint(w, `{"items":[{"_id":"id1","title":"voice"},{"_id":"id2","title":" "}]}`)
					}
				default:
					fmt.Fprint(w, `{"data":[{"id":"first"},{"id":"second"}]}`)
				}
			})
			driver := addedProviderDriver(t, provider, server.URL)
			names, err := driver.ListModels(addedProviderAPI())
			expected := []string{"first", "second"}
			if provider == "CoHere" {
				expected = []string{"first@cohere"}
			}
			if provider == "FishAudio" {
				expected = []string{"voice", " "}
			}
			if err != nil || !reflect.DeepEqual(modelNamesForTest(names), expected) {
				t.Fatal(names, err)
			}
			if err := driver.CheckConnection(addedProviderAPI()); err != nil {
				t.Fatal(err)
			}
			if provider == "FishAudio" {
				balance, err := driver.Balance(addedProviderAPI())
				if err != nil || balance["credit"] != float64(0) {
					t.Fatal(balance, err)
				}
			} else {
				if _, err := driver.Balance(addedProviderAPI()); err == nil {
					t.Fatal("unsupported balance succeeded")
				}
			}
		})
	}
	for _, provider := range []string{"CoHere", "FishAudio"} {
		for i, payload := range []string{`{}`, `null`, `{"error":{}}`, `{"models":[{}],"items":[{}]}`, `{"models":[],"items":[]} {}`} {
			t.Run(fmt.Sprintf("%s/bad-list-%d", provider, i), func(t *testing.T) {
				server := addedProviderServer(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, payload) })
				driver := addedProviderDriver(t, provider, server.URL)
				names, err := driver.ListModels(addedProviderAPI())
				if provider == "CoHere" && i == 3 {
					if err != nil || len(names) != 0 {
						t.Fatal("native blank-ID filter changed", names, err)
					}
				} else if err == nil || names != nil {
					t.Fatal(names, err)
				}
			})
		}
	}
	t.Run("native Cohere single page", func(t *testing.T) {
		calls := 0
		server := addedProviderServer(t, func(w http.ResponseWriter, r *http.Request) {
			calls++
			fmt.Fprint(w, `{"models":[],"next_page_token":"loop"}`)
		})
		names, err := addedProviderDriver(t, "CoHere", server.URL).ListModels(addedProviderAPI())
		if err != nil || len(names) != 0 || calls != 1 {
			t.Fatal("native one-page discovery changed", names, err, calls)
		}
	})
	for _, payload := range []string{`{}`, `null`, `{"credit":null}`, `{"credit":"0"}`, `{"credit":1e999}`, `{"credit":0,"error":{}}`} {
		t.Run("bad balance "+payload, func(t *testing.T) {
			server := addedProviderServer(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, payload) })
			if balance, err := addedProviderDriver(t, "FishAudio", server.URL).Balance(addedProviderAPI()); err == nil || balance != nil {
				t.Fatal(balance, err)
			}
		})
	}
}

func TestAddedProvidersConcurrentConfiguration(t *testing.T) {
	server := addedProviderServer(t, func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		json.NewEncoder(w).Encode(map[string]any{"data": []any{map[string]any{"index": 0, "embedding": []float64{0}}}})
	})
	driver := addedProviderDriver(t, "Upstage", server.URL)
	name := "solar-embedding-1-large-passage"
	var wg sync.WaitGroup
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			if _, err := driver.Embed(&name, []string{"a"}, addedProviderAPI(), &EmbeddingConfig{}); err != nil {
				t.Error(err)
			}
		}(i)
	}
	wg.Wait()
	if name != "solar-embedding-1-large-passage" {
		t.Fatal(name)
	}
}

func TestCoHereLargeStreamChunk(t *testing.T) {
	text := strings.Repeat("a", 128*1024)
	server := addedProviderServer(t, func(w http.ResponseWriter, r *http.Request) {
		data, _ := json.Marshal(map[string]any{"type": "content-delta", "delta": map[string]any{"message": map[string]any{"content": map[string]any{"text": text}}}})
		fmt.Fprintf(w, "data: %s\n\ndata: {\"type\":\"message-end\"}\n\n", data)
	})
	var got strings.Builder
	err := addedProviderDriver(t, "CoHere", server.URL).ChatStreamlyWithSender("m", []Message{{Role: "user", Content: "q"}}, addedProviderAPI(), nil, func(answer, reasoning *string) error {
		if answer != nil && *answer != "[DONE]" {
			got.WriteString(*answer)
		}
		return nil
	})
	if err != nil || got.String() != text {
		t.Fatal(got.Len(), err)
	}
}

func TestAddedProviderEmbeddingPartialFailureAndCancellation(t *testing.T) {
	for _, provider := range []string{"CoHere", "Baichuan", "Upstage"} {
		for _, mode := range []string{"provider-error", "dimension-drift", "cancel"} {
			t.Run(provider+"/"+mode, func(t *testing.T) {
				var calls atomic.Int32
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				server := addedProviderServer(t, func(w http.ResponseWriter, r *http.Request) {
					calls.Add(1)
					if mode == "cancel" {
						io.Copy(io.Discard, r.Body)
						w.WriteHeader(http.StatusOK)
						w.(http.Flusher).Flush()
						cancel()
						<-r.Context().Done()
						return
					}
					if mode == "provider-error" {
						http.Error(w, "failed", http.StatusBadGateway)
						return
					}
					var body map[string]any
					json.NewDecoder(r.Body).Decode(&body)
					field := "input"
					if provider == "CoHere" {
						field = "texts"
					}
					inputs := body[field].([]any)
					vectors := make([][]float64, len(inputs))
					data := make([]map[string]any, len(inputs))
					for i := range inputs {
						vectors[i] = []float64{0}
						if mode == "dimension-drift" && i == len(inputs)-1 {
							vectors[i] = []float64{0, 1}
						}
						data[i] = map[string]any{"index": i, "embedding": vectors[i]}
					}
					json.NewEncoder(w).Encode(map[string]any{"data": data, "embeddings": map[string]any{"float": vectors}})
				})
				api := addedProviderAPI()
				api.Context = ctx
				data, err := addedProviderDriver(t, provider, server.URL).Embed(addedProviderName("model"), make([]string, 33), api, nil)
				if data != nil || err == nil {
					t.Fatal("partial or canceled result exposed", data, err)
				}
				if mode == "cancel" {
					if !errors.Is(err, context.Canceled) || calls.Load() != 1 {
						t.Fatal(err, calls.Load())
					}
				} else if calls.Load() != 1 {
					t.Fatal(calls.Load())
				}
			})
		}
	}
}

func TestCoHereChatProviderErrorIsNotAnAnswer(t *testing.T) {
	for _, stream := range []bool{false, true} {
		t.Run(fmt.Sprint(stream), func(t *testing.T) {
			server := addedProviderServer(t, func(w http.ResponseWriter, r *http.Request) {
				if stream {
					fmt.Fprint(w, "data: {\"type\":\"content-delta\",\"delta\":{\"message\":{\"content\":{\"text\":\"partial\"}}}}\n\ndata: {\"type\":\"message-end\",\"delta\":{\"finish_reason\":\"ERROR_TOXIC\"}}\n\n")
				} else {
					fmt.Fprint(w, `{"message":{"content":[{"type":"text","text":"partial"}]},"finish_reason":"ERROR_TOXIC"}`)
				}
			})
			driver := addedProviderDriver(t, "CoHere", server.URL)
			if stream {
				done := false
				err := driver.ChatStreamlyWithSender("m", []Message{{Role: "user", Content: "q"}}, addedProviderAPI(), nil, func(answer, reasoning *string) error {
					if answer != nil && *answer == "[DONE]" {
						done = true
					}
					return nil
				})
				if err == nil || done {
					t.Fatal(err, done)
				}
			} else {
				if response, err := driver.ChatWithMessages("m", []Message{{Role: "user", Content: "q"}}, addedProviderAPI(), nil); err == nil || response != nil {
					t.Fatal(response, err)
				}
			}
		})
	}
}

func TestAddedProviderUnsupportedCapabilities(t *testing.T) {
	for _, provider := range []string{"FishAudio", "StepFun", "Baichuan", "Upstage"} {
		t.Run(provider, func(t *testing.T) {
			server := addedProviderServer(t, func(w http.ResponseWriter, r *http.Request) { t.Error("unsupported operation reached provider") })
			driver := addedProviderDriver(t, provider, server.URL)
			if response, err := driver.Rerank(addedProviderName("m"), "q", []string{"a"}, addedProviderAPI(), nil); err == nil || response != nil {
				t.Fatal(response, err)
			}
			if provider == "FishAudio" || provider == "StepFun" {
				if response, err := driver.Embed(addedProviderName("m"), []string{"a"}, addedProviderAPI(), nil); err == nil || response != nil {
					t.Fatal(response, err)
				}
			}
			if provider == "FishAudio" {
				if err := driver.ChatStreamlyWithSender("m", []Message{{Role: "user", Content: "q"}}, addedProviderAPI(), nil, func(*string, *string) error { t.Error("unsupported stream emitted data"); return nil }); err == nil {
					t.Fatal("unsupported stream succeeded")
				}
			}
		})
	}
}

func TestCoHereBufferedCancellationAndMalformedDelta(t *testing.T) {
	good := "data: {\"type\":\"content-delta\",\"delta\":{\"message\":{\"content\":{\"thinking\":\"reason\",\"text\":\"answer\"}}}}\n\n"
	terminal := "data: {\"type\":\"message-end\"}\n\n"
	for _, mode := range []string{"cancel in reasoning", "invalid after answer", "provider error after answer"} {
		t.Run(mode, func(t *testing.T) {
			payload := good + terminal
			if mode == "invalid after answer" {
				payload = good + "data: {\"type\":\"content-delta\",\"delta\":{}}\n\n" + terminal
			}
			if mode == "provider error after answer" {
				payload = good + "data: {\"error\":\"private-provider-body\"}\n\n" + terminal
			}
			peer := addedProviderServer(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, payload) })
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			api := addedProviderAPI()
			api.Context = ctx
			done, answers := 0, 0
			err := addedProviderDriver(t, "CoHere", peer.URL).ChatStreamlyWithSender("model", []Message{{Role: "user", Content: "q"}}, api, nil, func(answer, reason *string) error {
				if mode == "cancel in reasoning" && reason != nil {
					cancel()
				}
				if answer != nil {
					if *answer == "[DONE]" {
						done++
					} else {
						answers++
					}
				}
				return nil
			})
			if err == nil || done != 0 || strings.Contains(err.Error(), "private-provider-body") {
				t.Fatal("false completion or leaked provider error", err, done)
			}
			if mode == "cancel in reasoning" && (!errors.Is(err, context.Canceled) || answers != 0) {
				t.Fatal("buffered content delivered after caller cancellation", answers, err)
			}
		})
	}
}
