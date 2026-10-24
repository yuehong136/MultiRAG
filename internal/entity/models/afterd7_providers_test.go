package models

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestAfterD7HostedChatDiscoveryAndVectors(t *testing.T) {
	for _, provider := range []string{"302.AI", "JieKouAI"} {
		t.Run(provider, func(t *testing.T) {
			key, name := "controlled-key", "served-model"
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer "+key {
					t.Error("authentication lost")
				}
				if r.Method == http.MethodGet {
					if r.URL.Path != "/models" || r.ContentLength != 0 {
						t.Error("model discovery must be bodyless GET")
					}
					fmt.Fprint(w, `{"data":[{"id":"served-model"}]}`)
					return
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if body["model"] != name {
					t.Error("model identity lost", body)
				}
				switch r.URL.Path {
				case "/chat":
					if provider == "302.AI" {
						if body["thinking"].(map[string]any)["type"] != "disabled" || body["reasoning"].(map[string]any)["effort"] != "low" {
							t.Error("302 thinking/effort lost", body)
						}
					} else if body["enable_thinking"] != false {
						t.Error("explicit false lost", body)
					}
					messages := body["messages"].([]any)
					if len(messages) != 2 {
						t.Error("history lost")
					}
					if body["stream"] == true {
						if body["do_sample"] != false || r.Header.Get("Accept") != "text/event-stream" {
							t.Error("stream config lost")
						}
						w.Header().Set("Content-Type", "text/event-stream")
						fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"reason\"}}]}\ndata: {\"choices\":[{\"delta\":{\"content\":\"answer\"},\"finish_reason\":\"stop\"}]}\n")
					} else {
						fmt.Fprint(w, `{"choices":[{"message":{"content":"answer","reasoning_content":"reason"}}]}`)
					}
				case "/embed":
					if body["dimensions"] != float64(2) || body["query"] != nil {
						t.Error("embedding dimension/query contract lost", body)
					}
					fmt.Fprint(w, `{"data":[{"index":1,"embedding":[1,2]},{"index":0,"embedding":[0,1]}]}`)
				case "/rerank":
					if body["top_n"] != float64(2) || body["query"] != "query" {
						t.Error("rerank options lost", body)
					}
					fmt.Fprint(w, `{"results":[{"index":1,"relevance_score":0},{"index":0,"relevance_score":0.9}]}`)
				default:
					t.Error("unexpected path", r.URL.Path)
				}
			}))
			defer srv.Close()
			driver, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": srv.URL}, URLSuffix{Chat: "chat", Models: "models", Embedding: "embed", Rerank: "rerank"})
			if err != nil || driver.Name() == "dummy" {
				t.Fatal(driver, err)
			}
			api := &APIConfig{APIKey: &key}
			thinking, sample, effort := false, false, "low"
			config := &ChatConfig{Thinking: &thinking, DoSample: &sample, Effort: &effort}
			messages := []Message{{Role: "system", Content: "system"}, {Role: "user", Content: []map[string]any{{"type": "text", "text": "describe"}, {"type": "image_url", "image_url": map[string]any{"url": "https://files.invalid/image.png"}}}}}
			response, err := driver.ChatWithMessages(name, api, messages, config)
			if err != nil || *response.Answer != "answer" || *response.ReasoningContent != "reason" {
				t.Fatal(response, err)
			}
			var answer, reasoning string
			if err := driver.ChatStreamlyWithMessages(name, messages, api, config, func(c, r *string) error {
				if c != nil {
					answer += *c
				}
				if r != nil {
					reasoning += *r
				}
				return nil
			}); err != nil || answer != "answer[DONE]" || reasoning != "reason" {
				t.Fatal(answer, reasoning, err)
			}
			if names, err := driver.ListModels(api); err != nil || strings.Join(names, ",") != name {
				t.Fatal(names, err)
			}
			if err := driver.CheckConnection(api); err != nil {
				t.Fatal(err)
			}
			if vectors, err := driver.Embed(&name, []string{"one", "two"}, api, &EmbeddingConfig{Dimension: 2, Query: true}); err != nil || len(vectors) != 2 || vectors[0].Embedding[0] != 0 || vectors[1].Index != 1 {
				t.Fatal(vectors, err)
			}
			if result, err := driver.Rerank(&name, "query", []string{"one", "two"}, api, nil); err != nil || len(result.Data) != 2 || result.Data[1].RelevanceScore != 0 {
				t.Fatal(result, err)
			}
			if _, ok := driver.(TTSDriver); ok {
				t.Fatal("unsupported TTS capability exposed")
			}
		})
	}
}

func TestAfterD7HostedRejectMalformedVectorsAndDiscovery(t *testing.T) {
	for _, provider := range []string{"302.AI", "JieKouAI"} {
		for _, kind := range []string{"models", "embedding", "rerank"} {
			var responses []string
			switch kind {
			case "models":
				responses = []string{`{}`, `{"data":null}`, `{"data":[{"id":null}]}`, `{"data":[{"id":"same"},{"id":"same"}]}`, `{"code":1,"data":[]}`}
			case "embedding":
				responses = []string{`{"data":[]}`, `{"data":[{"index":0,"embedding":[null]}]}`, `{"data":[{"embedding":[0]}]}`, `{"data":[{"index":0,"embedding":[1e999]}]}`, `{"data":[{"index":1,"embedding":[0]}]}`}
			case "rerank":
				responses = []string{`{"results":[]}`, `{"results":[{"index":0,"relevance_score":null}]}`, `{"results":[{"relevance_score":0}]}`, `{"results":[{"index":2,"relevance_score":0}]}`, `{"results":[{"index":0,"relevance_score":1e999}]}`}
			}
			for index, response := range responses {
				t.Run(fmt.Sprintf("%s/%s/%d", provider, kind, index), func(t *testing.T) {
					key, name := "key", "model"
					srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, response) }))
					defer srv.Close()
					driver, _ := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": srv.URL}, URLSuffix{Models: "models", Embedding: "embed", Rerank: "rerank"})
					var err error
					switch kind {
					case "models":
						_, err = driver.ListModels(&APIConfig{APIKey: &key})
					case "embedding":
						_, err = driver.Embed(&name, []string{"one"}, &APIConfig{APIKey: &key}, nil)
					case "rerank":
						_, err = driver.Rerank(&name, "query", []string{"one"}, &APIConfig{APIKey: &key}, nil)
					}
					if err == nil {
						t.Fatal("malformed result accepted", response)
					}
				})
			}
		}
	}
}

func TestAfterD7ASROCRAndParse(t *testing.T) {
	key, asrName, ocrName, parseName := "key", "whisper-v3-turbo", "mistral-ocr-latest", "vlm"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+key {
			t.Error("media credential lost")
		}
		switch r.URL.Path {
		case "/asr":
			if err := r.ParseMultipartForm(1024); err != nil {
				t.Error(err)
				return
			}
			defer r.MultipartForm.RemoveAll()
			file, _, err := r.FormFile("file")
			if err != nil {
				t.Error(err)
				return
			}
			defer file.Close()
			data, _ := io.ReadAll(file)
			if string(data) != "owned audio" || r.FormValue("model") != asrName || r.FormValue("temperature") != "0" {
				t.Error("multipart media lost")
			}
			fmt.Fprint(w, `{"text":"recognized"}`)
		case "/ocr":
			var body struct {
				Model    string            `json:"model"`
				Document map[string]string `json:"document"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Model != ocrName || body.Document["type"] != "document_url" || !(strings.HasPrefix(body.Document["document_url"], "data:application/pdf;base64,") || body.Document["document_url"] == "https://files.invalid/doc.pdf") {
				t.Error("OCR URL/data lost", body, err)
			}
			fmt.Fprint(w, `{"pages":[{"index":1,"markdown":"second"},{"index":0,"markdown":"first"}]}`)
		case "/parse":
			fmt.Fprint(w, `{"code":0,"data":{"task_id":"job-1"}}`)
		case "/query/job-1":
			fmt.Fprint(w, `{"code":0,"data":{"task_id":"job-1","state":"done","full_zip_url":"https://files.invalid/result.zip"}}`)
		default:
			t.Error("unexpected media path", r.URL.Path)
		}
	}))
	defer srv.Close()
	m := NewAI302Model(map[string]string{"default": srv.URL}, URLSuffix{ASR: "asr", OCR: "ocr", DocumentParse: "parse", Task: "query"})
	api := &APIConfig{APIKey: &key}
	if result, err := m.TranscribeAudio(&asrName, []byte("owned audio"), api, &ASRConfig{Params: map[string]any{"temperature": 0}}); err != nil || *result.Text != "recognized" {
		t.Fatal(result, err)
	}
	if result, err := m.OCRFile(&ocrName, []byte("%PDF-1.7 owned"), api, nil); err != nil || *result.Text != "first\n\nsecond" {
		t.Fatal(result, err)
	}
	if result, err := m.OCRURL(&ocrName, "https://files.invalid/doc.pdf", api, nil); err != nil || *result.Text != "first\n\nsecond" {
		t.Fatal(result, err)
	}
	if result, err := m.ParseFile(&parseName, nil, "https://files.invalid/doc.pdf", api); err != nil || result.Status != "pending" || len(result.Segments) != 0 {
		t.Fatal(result, err)
	}
	if result, err := m.ShowTask("job-1", api); err != nil || result.Status != "success" || result.Segments[0].Content != "https://files.invalid/result.zip" {
		t.Fatal(result, err)
	}
	if _, err := m.ListTasks(api); !errors.Is(err, ErrUnsupportedCapability) {
		t.Fatal(err)
	}
}

func TestAfterD7CometAPISpeechBinaryAndOptions(t *testing.T) {
	key, name := "key", "tts-1"
	audio := []byte("RIFF-owned-wave-bytes")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body["model"] != name || body["input"] != "speak" || body["voice"] != "alloy" || body["response_format"] != "wav" || r.Header.Get("Authorization") != "Bearer key" {
			t.Error("speech binding/options lost", body)
		}
		w.Header().Set("Content-Type", "audio/wav")
		w.Write(audio)
	}))
	defer srv.Close()
	m := NewCometAPIModel(map[string]string{"default": srv.URL}, URLSuffix{TTS: "speech"})
	result, err := m.AudioSpeech(&name, "speak", &APIConfig{APIKey: &key}, &TTSConfig{Voice: "alloy", Format: "wav", Params: map[string]any{"speed": 0.8}})
	if err != nil || result.Format != "wav" || !bytes.Equal(result.Audio, audio) {
		t.Fatal(result, err)
	}
	if _, ok := any(m).(TTSStreamDriver); ok {
		t.Fatal("unsupported speech stream exposed")
	}
	var calls atomic.Int64
	blocked := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1) }))
	defer blocked.Close()
	m = NewCometAPIModel(map[string]string{"default": blocked.URL}, URLSuffix{ASR: "asr", TTS: "tts"})
	for _, field := range []string{"model", "input", "api_key", "authorization", "base_url", "stream"} {
		if _, err := m.AudioSpeech(&name, "speak", &APIConfig{APIKey: &key}, &TTSConfig{Params: map[string]any{field: "override"}}); err == nil {
			t.Fatal("protected field accepted", field)
		}
		if _, err := m.TranscribeAudio(&name, []byte("audio"), &APIConfig{APIKey: &key}, &ASRConfig{Params: map[string]any{field: "override"}}); err == nil {
			t.Fatal("protected ASR field accepted", field)
		}
	}
	if calls.Load() != 0 {
		t.Fatal("invalid media params called provider")
	}
}

func TestAfterD7ProviderCancellationAndMediaErrors(t *testing.T) {
	key, name := "key", "model"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/wait" {
			io.Copy(io.Discard, r.Body)
			<-r.Context().Done()
			return
		}
		fmt.Fprint(w, `{"error":"private-provider-body"}`)
	}))
	defer srv.Close()
	m := NewAI302Model(map[string]string{"default": srv.URL}, URLSuffix{ASR: "asr", OCR: "ocr"})
	if _, err := m.TranscribeAudio(&name, []byte("audio"), &APIConfig{APIKey: &key}, nil); err == nil || strings.Contains(err.Error(), "private-provider-body") {
		t.Fatal("ASR provider failure lost/leaked", err)
	}
	if _, err := m.OCRURL(&name, "https://files.invalid/doc.pdf", &APIConfig{APIKey: &key}, nil); err == nil || strings.Contains(err.Error(), "private-provider-body") {
		t.Fatal("OCR provider failure lost/leaked", err)
	}
	c := NewCometAPIModel(map[string]string{"default": srv.URL}, URLSuffix{TTS: "speech", ASR: "wait"})
	if _, err := c.AudioSpeech(&name, "speak", &APIConfig{APIKey: &key}, nil); err == nil {
		t.Fatal("JSON speech response accepted")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Millisecond)
	defer cancel()
	if _, err := c.TranscribeAudio(&name, []byte("audio"), &APIConfig{APIKey: &key, Context: ctx}, nil); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("ASR cancellation lost", err)
	}
}
