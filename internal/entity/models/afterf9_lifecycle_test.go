package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestAfterf9MediaInFlightDeadlineAndIsolation(t *testing.T) {
	for _, p := range []struct{ provider, kind string }{{"Astraflow", "tts"}, {"Groq", "tts"}, {"Groq", "asr"}, {"OpenAI", "asr"}, {"OpenAI", "tts"}, {"PaddleOCR_Local", "ocr"}, {"ZHIPU-AI", "ocr"}} {
		t.Run(p.provider+p.kind, func(t *testing.T) {
			started, stopped := make(chan struct{}), make(chan struct{})
			srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				io.Copy(io.Discard, r.Body)
				if p.kind == "tts" {
					w.Header().Set("Content-Type", "audio/mpeg")
				}
				w.WriteHeader(200)
				w.(http.Flusher).Flush()
				close(started)
				select {
				case <-r.Context().Done():
				case <-time.After(2 * time.Second):
					t.Error("request not canceled")
				}
				close(stopped)
			}))
			defer srv.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
			defer cancel()
			d := afterf9Driver(t, p.provider, srv.URL)
			api := &APIConfig{APIKey: stringPtr("key"), Context: ctx}
			var err error
			switch p.kind {
			case "asr":
				_, err = d.(ASRDriver).TranscribeAudio(stringPtr("served"), []byte("audio"), api, nil)
			case "tts":
				_, err = d.(TTSDriver).AudioSpeech(stringPtr("served"), "hello", api, &TTSConfig{Voice: "voice"})
			default:
				_, err = d.(OCRDriver).OCRFile(stringPtr("served"), []byte("image"), api, nil)
			}
			if !errors.Is(err, context.DeadlineExceeded) {
				t.Fatal(err)
			}
			select {
			case <-started:
			default:
				t.Fatal("fixture was not reached")
			}
			select {
			case <-stopped:
			case <-time.After(time.Second):
				t.Fatal("request leaked")
			}
		})
	}
}

func TestAfterf9ConcurrentInstancesAndRegions(t *testing.T) {
	for _, provider := range []string{"Astraflow", "HunYuan", "TokenHub", "Groq", "OpenAI", "PaddleOCR_Local", "ZHIPU-AI"} {
		t.Run(provider, func(t *testing.T) {
			var calls atomic.Int32
			makeServer := func(key string) *httptest.Server {
				return afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					calls.Add(1)
					if r.Header.Get("Authorization") != "Bearer "+key {
						t.Error("credential crossed instances")
					}
					if provider == "PaddleOCR_Local" {
						fmt.Fprintf(w, `{"errorCode":0,"result":{"layoutParsingResults":[{"markdown":{"text":%q}}]}}`, key)
						return
					}
					if provider == "ZHIPU-AI" {
						fmt.Fprintf(w, `{"md_results":%q}`, key)
						return
					}
					if provider == "Groq" {
						fmt.Fprintf(w, `{"text":%q}`, key)
						return
					}
					var b map[string]any
					if err := json.NewDecoder(r.Body).Decode(&b); err != nil {
						t.Error(err)
						return
					}
					if b["model"] != "served-"+key {
						t.Error("model identity crossed", b)
					}
					fmt.Fprintf(w, `{"data":[{"index":0,"embedding":[%d,0]}]}`, len(key))
				}))
			}
			first, second := makeServer("one"), makeServer("second")
			defer first.Close()
			defer second.Close()
			base := map[string]string{"office": first.URL, "other": second.URL}
			d, _ := NewModelFactory().CreateModelDriver(provider, base, URLSuffix{Embedding: "embeddings", OCR: "ocr", ASR: "asr"})
			base["office"] = "http://mutated.invalid"
			var wg sync.WaitGroup
			for i := 0; i < 12; i++ {
				wg.Add(1)
				go func(i int) {
					defer wg.Done()
					key, region := "one", "office"
					if i%2 == 1 {
						key, region = "second", "other"
					}
					api := &APIConfig{APIKey: &key, Region: &region}
					name := "served-" + key
					if provider == "PaddleOCR_Local" || provider == "ZHIPU-AI" {
						result, err := d.(OCRDriver).OCRFile(&name, []byte("image"), api, nil)
						if err != nil || result == nil || *result.Text != key {
							t.Error(result, err)
						}
					} else if provider == "Groq" {
						result, err := d.(ASRDriver).TranscribeAudio(&name, []byte("audio"), api, nil)
						if err != nil || result == nil || *result.Text != key {
							t.Error(result, err)
						}
					} else {
						result, err := d.Embed(&name, []string{"text"}, api, nil)
						if err != nil || len(result) != 1 || result[0].Embedding[0] != float64(len(key)) {
							t.Error(result, err)
						}
					}
				}(i)
			}
			wg.Wait()
			if calls.Load() != 12 {
				t.Fatal(calls.Load())
			}
		})
	}
}

func TestAfterf9MediaRejectRedirect(t *testing.T) {
	var forwarded atomic.Int32
	target := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		forwarded.Add(1)
		w.Header().Set("Content-Type", "audio/mpeg")
		fmt.Fprint(w, "bytes")
	}))
	defer target.Close()
	redirect := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL, http.StatusTemporaryRedirect)
	}))
	defer redirect.Close()
	for _, provider := range []string{"Astraflow", "Groq", "OpenAI"} {
		_, err := afterf9Driver(t, provider, redirect.URL).(TTSDriver).AudioSpeech(stringPtr("served"), "hello", &APIConfig{APIKey: stringPtr("key")}, &TTSConfig{Voice: "voice"})
		if err == nil || !strings.Contains(err.Error(), "307") {
			t.Fatal(provider, err)
		}
	}
	if forwarded.Load() != 0 {
		t.Fatal("redirect forwarded credentials/media", forwarded.Load())
	}
}

var afterf9ResourceLock sync.Mutex

func afterf9Server(t *testing.T, h http.Handler) *httptest.Server {
	t.Helper()
	record := func(value map[string]any) {
		t.Helper()
		path := os.Getenv("MULTIRAG_MODEL_TEST_RESOURCES")
		if path == "" {
			return
		}
		afterf9ResourceLock.Lock()
		defer afterf9ResourceLock.Unlock()
		file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
		if err != nil {
			t.Fatal(err)
		}
		defer file.Close()
		value["test"] = t.Name()
		value["pid"] = os.Getpid()
		value["process"] = os.Args[0]
		if err := json.NewEncoder(file).Encode(value); err != nil {
			t.Fatal(err)
		}
	}
	record(map[string]any{"action": "intent", "listen": "127.0.0.1:0"})
	server := httptest.NewUnstartedServer(h)
	address := server.Listener.Addr().String()
	record(map[string]any{"action": "bound", "address": address})
	server.Start()
	t.Cleanup(func() {
		server.CloseClientConnections()
		server.Close()
		conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
		if err == nil {
			conn.Close()
			t.Error("owned listener still reachable", address)
		}
		record(map[string]any{"action": "closed-readback", "address": address, "absent": err != nil})
	})
	return server
}
