package models

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

var providerJournalMu sync.Mutex

func providerResourceRecord(t *testing.T, value map[string]any) {
	t.Helper()
	path := os.Getenv("MULTIRAG_PROVIDER_TEST_JOURNAL")
	if path == "" {
		if os.Getenv("MULTIRAG_RUN_ID") != "" {
			t.Fatal("registered run requires provider resource journal")
		}
		return
	}
	providerJournalMu.Lock()
	defer providerJournalMu.Unlock()
	flags := os.O_APPEND | os.O_WRONLY
	if os.Getenv("MULTIRAG_RUN_ID") == "" {
		flags |= os.O_CREATE
	}
	f, err := os.OpenFile(path, flags, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	value["case"] = t.Name()
	value["parent_run_id"] = os.Getenv("MULTIRAG_RUN_ID")
	if err := json.NewEncoder(f).Encode(value); err != nil {
		t.Fatal(err)
	}
	if err := f.Sync(); err != nil {
		t.Fatal(err)
	}
}

// A fresh journal identity precedes listener allocation. Cleanup independently
// proves the exact bound address no longer accepts connections.
func providerTestServer(t *testing.T, handler http.Handler) *httptest.Server {
	t.Helper()
	var listener net.Listener
	var run string
	for attempt := 0; attempt < 10; attempt++ {
		token := make([]byte, 16)
		if _, err := rand.Read(token); err != nil {
			t.Fatal(err)
		}
		run = hex.EncodeToString(token)
		address := fmt.Sprintf("127.0.0.1:%d", 20000+(int(token[0])*256+int(token[1]))%30000)
		providerResourceRecord(t, map[string]any{"action": "intent", "run_id": run, "kind": "http-listener", "address": address})
		// macOS can allow this bind alongside an existing wildcard listener.
		// Check before allocation so closing our socket cannot reveal an
		// unrelated application and misreport it as an owned listener leak.
		if conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond); err == nil {
			conn.Close()
			providerResourceRecord(t, map[string]any{"action": "occupied-borrowed", "run_id": run, "kind": "http-listener", "address": address})
			continue
		}
		var err error
		listener, err = net.Listen("tcp", address)
		if err == nil {
			break
		}
		providerResourceRecord(t, map[string]any{"action": "allocation-unavailable", "run_id": run, "kind": "http-listener", "address": address})
	}
	if listener == nil {
		t.Fatal("no unoccupied provider test address")
	}
	t.Cleanup(func() { listener.Close() })
	addr := listener.Addr().String()
	providerResourceRecord(t, map[string]any{"action": "allocated", "run_id": run, "kind": "http-listener", "address": addr})
	srv := &httptest.Server{Listener: listener, Config: &http.Server{Handler: handler}}
	t.Cleanup(func() {
		srv.Close()
		conn, err := net.DialTimeout("tcp", addr, 100*time.Millisecond)
		absent := err != nil
		if conn != nil {
			conn.Close()
		}
		providerResourceRecord(t, map[string]any{"action": "cleanup-readback", "run_id": run, "kind": "http-listener", "address": addr, "absent": absent})
		if !absent {
			t.Error("provider listener remains", addr)
		}
	})
	srv.Start()
	return srv
}

func TestProviderFixDiscoveryRawHTTP(t *testing.T) {
	for _, provider := range []string{"LongCat", "xAI", "VolcEngine", "TokenHub"} {
		for _, raw := range []string{`{"data":[{"id":"one","owned_by":"meta"},{"id":"two"}]}`, `{"data":[]}`, `{}`, `{"data":null}`, `{"data":{}}`, `{"data":[null]}`, `{"data":[{"id":null}]}`, `{"data":[{"id":3}]}`, `{"data":[{"id":" one"}]}`, `{"data":[{"id":"one"},{"id":"one"}]}`, `{"data":[],"error":{}}`, `{"data":[]} {}`, `{"data":[{"id":"one"}]} trailing`} {
			t.Run(provider+"/"+raw, func(t *testing.T) {
				srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if r.Method != "GET" || r.URL.Path != "/v1/models" || r.Header.Get("Authorization") != "Bearer key" || r.ContentLength > 0 {
						t.Error("discovery request", r.Method, r.URL.Path, r.ContentLength)
					}
					fmt.Fprint(w, raw)
				}))
				driver, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": srv.URL + "/v1/"}, URLSuffix{Models: "/models/"})
				if err != nil {
					t.Fatal(err)
				}
				result, err := driver.ListModels(&APIConfig{APIKey: stringPtr("key")})
				valid := raw == `{"data":[]}` || strings.Contains(raw, `"owned_by"`)
				if (err == nil) != valid {
					t.Fatal(result, err)
				}
				if valid && result == nil {
					t.Fatal("explicit empty lost")
				}
				if strings.Contains(raw, `"owned_by"`) && !reflect.DeepEqual(result, []string{"one", "two"}) {
					t.Fatal(result)
				}
			})
		}
	}
}

func TestProviderFixDiscoveryDenialAndCancellation(t *testing.T) {
	for _, provider := range []string{"LongCat", "xAI", "VolcEngine", "TokenHub"} {
		t.Run(provider, func(t *testing.T) {
			var calls atomic.Int32
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				w.WriteHeader(200)
				w.(http.Flusher).Flush()
				<-r.Context().Done()
			}))
			driver, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": srv.URL}, URLSuffix{Models: "models"})
			for _, api := range []*APIConfig{nil, {}, {APIKey: stringPtr(" ")}} {
				if _, err := driver.ListModels(api); err == nil {
					t.Fatal("missing auth accepted")
				}
			}
			if calls.Load() != 0 {
				t.Fatal("denial reached HTTP")
			}
			ctx, cancel := context.WithTimeout(t.Context(), 60*time.Millisecond)
			defer cancel()
			if _, err := driver.ListModels(&APIConfig{APIKey: stringPtr("key"), Context: ctx}); err == nil {
				t.Fatal("cancel accepted")
			}
			empty, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": srv.URL}, URLSuffix{Models: " / "})
			before := calls.Load()
			if _, err := empty.ListModels(&APIConfig{APIKey: stringPtr("key"), Context: ctx}); err == nil {
				t.Fatal("blank suffix accepted")
			}
			if calls.Load() != before {
				t.Fatal("blank suffix reached HTTP")
			}
		})
	}
}

func TestProviderFixReplicateRerank(t *testing.T) {
	for _, versioned := range []bool{false, true} {
		for _, wrapped := range []bool{false, true} {
			for _, top := range []int{0, 1, 3, 9} {
				t.Run(fmt.Sprint(versioned, wrapped, top), func(t *testing.T) {
					calls := 0
					name := "owner/model"
					version := strings.Repeat("b", 64)
					if versioned {
						name += ":" + version
					}
					srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						calls++
						if r.Header.Get("Authorization") != "Bearer key" {
							t.Error("auth")
						}
						if r.Method == "POST" {
							path := "/v1/models/owner/model/predictions"
							if versioned {
								path = "/v1/predictions"
							}
							if r.URL.Path != path || r.Header.Get("Prefer") != "wait=60" {
								t.Error("route", r.URL.Path)
							}
							var body struct {
								Version string
								Stream  bool
								Input   map[string]any
							}
							if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
								t.Error(err)
							}
							if versioned && body.Version != version || !versioned && body.Version != "" || body.Stream {
								t.Error("version/stream", body)
							}
							if body.Input["input_list"] != `[["q","a"],["q","b"],["q","c"]]` || len(body.Input) != 1 {
								t.Error(body)
							}
							fmt.Fprintf(w, `{"id":"pred-1","status":"processing","urls":{"get":"http://%s/v1/predictions/pred-1"}}`, r.Host)
						} else {
							if r.Method != "GET" || r.URL.Path != "/v1/predictions/pred-1" {
								t.Error("poll route")
							}
							raw := `[0,2,2]`
							if wrapped {
								raw = `{"scores":[0,2,2]}`
							}
							fmt.Fprintf(w, `{"id":"pred-1","status":"succeeded","output":%s}`, raw)
						}
					}))
					result, err := newReplicateTestModel(srv.URL).Rerank(&name, "q", []string{"a", "b", "c"}, &APIConfig{APIKey: stringPtr("key")}, &RerankConfig{TopN: top})
					expected := 3
					if top == 1 {
						expected = 1
					}
					if err != nil || calls != 2 || len(result.Data) != expected || result.Data[0].Index != 1 {
						t.Fatal(result, err, calls)
					}
					if expected == 3 && (result.Data[2].Index != 0 || result.Data[2].RelevanceScore != 0) {
						t.Fatal(result)
					}
				})
			}
		}
	}
}

func TestProviderFixReplicateRerankRawAndTerminal(t *testing.T) {
	for _, raw := range []string{`null`, `[]`, `[null]`, `["0"]`, `[1e999]`, `{}`, `{"scores":null}`, `{"scores":[null]}`, `[0,1]`, `{"scores":[]}`} {
		t.Run(raw, func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				fmt.Fprintf(w, `{"id":"p","status":"succeeded","output":%s}`, raw)
			}))
			data, err := newReplicateTestModel(srv.URL).Rerank(stringPtr("owner/model"), "q", []string{"a"}, &APIConfig{APIKey: stringPtr("key")}, nil)
			if err == nil || data != nil {
				t.Fatal(data, err)
			}
		})
	}
	for _, status := range []string{"failed", "canceled", "unknown"} {
		t.Run(status, func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				fmt.Fprintf(w, `{"id":"p","status":%q,"output":[0]}`, status)
			}))
			if data, err := newReplicateTestModel(srv.URL).Rerank(stringPtr("owner/model"), "q", []string{"a"}, &APIConfig{APIKey: stringPtr("key")}, nil); err == nil || data != nil {
				t.Fatal(data, err)
			}
		})
	}
	t.Run("cancel", func(t *testing.T) {
		srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Method == "POST" {
				fmt.Fprintf(w, `{"id":"p","status":"processing","urls":{"get":"http://%s/p"}}`, r.Host)
				return
			}
			w.WriteHeader(200)
			w.(http.Flusher).Flush()
			<-r.Context().Done()
		}))
		ctx, cancel := context.WithTimeout(t.Context(), 60*time.Millisecond)
		defer cancel()
		if _, err := newReplicateTestModel(srv.URL).Rerank(stringPtr("owner/model"), "q", []string{"a"}, &APIConfig{APIKey: stringPtr("key"), Context: ctx}, nil); !errors.Is(err, context.DeadlineExceeded) {
			t.Fatal(err)
		}
	})
}

func TestProviderFixTokenHubZeroAndInvalid(t *testing.T) {
	var calls atomic.Int32
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body["stream"] != false || body["temperature"] != float64(0) || body["top_p"] != float64(0) || len(body["messages"].([]any)) != 2 {
			t.Error(body)
		}
		fmt.Fprint(w, `{"choices":[{"message":{"content":"ok","reasoning_content":"thought"}}]}`)
	}))
	d := NewTokenHubModel(map[string]string{"default": srv.URL}, URLSuffix{Chat: "chat"})
	zero := 0.0
	yes := true
	messages := []Message{{Role: "system", Content: "rules"}, {Role: "user", Content: "q"}}
	result, err := d.ChatWithMessages("model", &APIConfig{APIKey: stringPtr("key")}, messages, &ChatConfig{Temperature: &zero, TopP: &zero, Stream: &yes})
	if err != nil || *result.Answer != "ok" {
		t.Fatal(result, err)
	}
	for _, temp := range []float64{math.NaN(), math.Inf(1), -1, 3} {
		if _, err := d.ChatWithMessages("model", &APIConfig{APIKey: stringPtr("key")}, messages, &ChatConfig{Temperature: &temp}); err == nil {
			t.Fatal("invalid temperature")
		}
	}
	for _, messages := range [][]Message{nil, {{Role: "invalid", Content: "q"}}, {{Role: "user", Content: 3}}} {
		if _, err := d.ChatWithMessages("model", &APIConfig{APIKey: stringPtr("key")}, messages, nil); err == nil {
			t.Fatal("invalid messages")
		}
	}
	if calls.Load() != 1 {
		t.Fatal("invalid input reached provider", calls.Load())
	}
}

func TestProviderFixXAIAudio(t *testing.T) {
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer key" {
			t.Error("auth")
		}
		if r.URL.Path == "/stt" {
			if err := r.ParseMultipartForm(MaxMediaBytes); err != nil {
				t.Error(err)
				return
			}
			defer r.MultipartForm.RemoveAll()
			f, _, err := r.FormFile("file")
			if err != nil {
				t.Error(err)
				return
			}
			defer f.Close()
			data, _ := io.ReadAll(f)
			if string(data) != "wav-data" || r.FormValue("model") != "stt" || r.FormValue("language") != "zh" || r.FormValue("temperature") != "0" {
				t.Error("multipart fields")
			}
			fmt.Fprint(w, `{"text":"识别结果"}`)
			return
		}
		if r.URL.Path != "/tts" {
			t.Error("speech path", r.URL.Path)
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body["text"] != "hello" || body["voice_id"] != "eve" || body["output_format"].(map[string]any)["codec"] != "mp3" || body["model"] != nil {
			t.Error(body)
		}
		w.Header().Set("Content-Type", "audio/mpeg")
		w.Write([]byte("mp3-data"))
	}))
	x := NewXAIModel(map[string]string{"default": srv.URL}, URLSuffix{ASR: "stt", TTS: "tts"})
	api := &APIConfig{APIKey: stringPtr("key")}
	asr, err := x.TranscribeAudio(stringPtr("stt"), []byte("wav-data"), api, &ASRConfig{Language: []string{"zh"}, Params: map[string]any{"temperature": float64(0)}})
	if err != nil || *asr.Text != "识别结果" {
		t.Fatal(asr, err)
	}
	speech, err := x.AudioSpeech(stringPtr("eve"), "hello", api, nil)
	if err != nil || string(speech.Audio) != "mp3-data" {
		t.Fatal(speech, err)
	}
	var data []byte
	if err := x.AudioSpeechWithSender(stringPtr("eve"), "hello", api, nil, func(r *TTSResponse) error { data = append(data, r.Audio...); return nil }); err != nil || string(data) != "mp3-data" {
		t.Fatal(err, string(data))
	}
	marker := errors.New("sender stopped")
	if err := x.AudioSpeechWithSender(stringPtr("eve"), "hello", api, nil, func(*TTSResponse) error { return marker }); !errors.Is(err, marker) {
		t.Fatal(err)
	}
}

func TestProviderFixXAIAudioFailures(t *testing.T) {
	for _, typ := range []string{"application/json", "text/event-stream", "audio/wav", "audio/mpeg"} {
		t.Run(typ, func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", typ)
				if typ != "audio/mpeg" {
					fmt.Fprint(w, `{"error":"invalid"}`)
				}
			}))
			x := NewXAIModel(map[string]string{"default": srv.URL}, URLSuffix{TTS: "tts"})
			calls := 0
			if err := x.AudioSpeechWithSender(stringPtr("eve"), "hello", &APIConfig{APIKey: stringPtr("key")}, nil, func(*TTSResponse) error { calls++; return nil }); err == nil || calls != 0 {
				t.Fatal(err, calls)
			}
		})
	}
	t.Run("cancel", func(t *testing.T) {
		srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "audio/mpeg")
			w.WriteHeader(200)
			w.(http.Flusher).Flush()
			<-r.Context().Done()
		}))
		ctx, cancel := context.WithTimeout(t.Context(), 60*time.Millisecond)
		defer cancel()
		x := NewXAIModel(map[string]string{"default": srv.URL}, URLSuffix{TTS: "tts"})
		if err := x.AudioSpeechWithSender(stringPtr("eve"), "hello", &APIConfig{APIKey: stringPtr("key"), Context: ctx}, nil, func(*TTSResponse) error { return nil }); !errors.Is(err, context.DeadlineExceeded) {
			t.Fatal(err)
		}
	})
}

func TestProviderFixRegionalInstanceIsolation(t *testing.T) {
	for _, provider := range []string{"Gitee", "xAI", "LongCat", "VolcEngine", "TokenHub"} {
		t.Run(provider, func(t *testing.T) {
			var calls atomic.Int32
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.URL.Path != "/china/v1/chat/completions" || r.Header.Get("Authorization") != "Bearer china-key" {
					t.Error("region identity", r.URL.Path)
				}
				fmt.Fprint(w, `{"choices":[{"message":{"content":"ok"}}]}`)
			}))
			urls := map[string]string{"default": "https://unused.invalid", "china": srv.URL + "/china/v1"}
			d, err := NewModelFactory().CreateModelDriver(provider, urls, URLSuffix{Chat: "chat/completions"})
			if err != nil {
				t.Fatal(err)
			}
			urls["china"] = "https://mutated.invalid"
			region := "china"
			api := &APIConfig{APIKey: stringPtr("china-key"), Region: &region}
			if _, err := d.ChatWithMessages("m", api, []Message{{Role: "user", Content: "q"}}, nil); err != nil {
				t.Fatal(err)
			}
			if calls.Load() != 1 {
				t.Fatal(calls.Load())
			}
		})
	}
}

func providerTestTempDir(t *testing.T) string {
	t.Helper()
	token := make([]byte, 16)
	if _, err := rand.Read(token); err != nil {
		t.Fatal(err)
	}
	run := hex.EncodeToString(token)
	path := os.TempDir() + "/provider-case-" + run
	providerResourceRecord(t, map[string]any{"action": "intent", "run_id": run, "kind": "temporary-directory", "path": path})
	if err := os.Mkdir(path, 0700); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(path); err != nil {
			t.Error(err)
		}
		_, err := os.Stat(path)
		providerResourceRecord(t, map[string]any{"action": "cleanup-readback", "run_id": run, "kind": "temporary-directory", "path": path, "absent": os.IsNotExist(err)})
	})
	return path
}
