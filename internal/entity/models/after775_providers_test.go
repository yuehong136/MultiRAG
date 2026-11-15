package models

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"

	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func after775Driver(t *testing.T, provider, base string) ModelDriver {
	t.Helper()
	d, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": base}, URLSuffix{Chat: "chat", Models: "models", ASR: "asr", TTS: "tts", Rerank: "rerank"})
	if err != nil || d.Name() == "dummy" {
		t.Fatalf("driver %T %v", d, err)
	}
	return d
}

func TestAfter775Gateways(t *testing.T) {
	for _, provider := range []string{"PPIO", "TokenPony", "Groq"} {
		t.Run(provider, func(t *testing.T) {
			var response, contentType string
			var calls atomic.Int32
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.Header.Get("Authorization") != "Bearer key" {
					t.Error("credentials lost")
				}
				if r.Method == http.MethodPost {
					var b map[string]any
					if err := json.NewDecoder(r.Body).Decode(&b); err != nil {
						t.Error(err)
					}
					if b["model"] != "served" || b["temperature"] != float64(0) || b["top_p"] != float64(0) || b["reasoning_effort"] != nil {
						t.Error("body contract", b)
					}
					if len(b["messages"].([]any)) != 3 {
						t.Error("history lost")
					}
				}
				w.Header().Set("Content-Type", contentType)
				fmt.Fprint(w, response)
			}))
			defer srv.Close()
			d := after775Driver(t, provider, srv.URL)
			key, name, zero, effort := "key", "served", 0.0, "high"
			api := &APIConfig{ApiKey: &key}
			cfg := &ChatConfig{Temperature: &zero, TopP: &zero, Effort: &effort}
			messages := []Message{{Role: "system", Content: "system"}, {Role: "assistant", Content: "old"}, {Role: "user", Content: "question"}}
			response = `{"choices":[{"message":{"content":"answer","reasoning_content":"primary","reasoning":"alias"}}]}`
			r, err := d.ChatWithMessages(name, messages, api, cfg)
			if err != nil || *r.Answer != "answer" || *r.ReasonContent != "primary" {
				t.Fatal(r, err)
			}
			for _, raw := range []string{`{}`, `{"choices":[]}`, `{"choices":[{}]}`, `{"choices":[{"message":{"content":42}}]}`, `{"error":{"message":"private-provider-body"}}`, `{"code":9,"choices":[{"message":{"content":"bad"}}]}`} {
				response = raw
				if _, err := d.ChatWithMessages(name, messages, api, cfg); err == nil || strings.Contains(err.Error(), "private-provider-body") {
					t.Fatal("malformed chat accepted/leaked", raw, err)
				}
			}
			contentType = "text/event-stream"
			response = "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"primary\",\"reasoning\":\"alias\"}}]}\n\ndata: {\"choices\":[{\"delta\":{\"content\":\"answer\"}}]}\n\ndata: {\"choices\":[{\"finish_reason\":\"stop\"}]}\n\n"
			var answer, reason string
			err = d.ChatStreamlyWithSender(name, messages, api, cfg, func(a, r *string) error {
				if a != nil {
					answer += *a
				}
				if r != nil {
					reason += *r
				}
				return nil
			})
			if err != nil || answer != "answer[DONE]" || reason != "primary" {
				t.Fatal(answer, reason, err)
			}
			for _, raw := range []string{"data: {oops}\n\n", "data: {\"choices\":[]}\n\n", "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n", "data: {\"error\":\"private-provider-body\"}\n\n", "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"content_filter\"}]}\n\n"} {
				response = raw
				done := false
				err = d.ChatStreamlyWithSender(name, messages, api, cfg, func(a, r *string) error { done = done || a != nil && *a == "[DONE]"; return nil })
				if err == nil || done || strings.Contains(err.Error(), "private-provider-body") {
					t.Fatal("false completion", raw, err, done)
				}
			}
			response = "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"reason\",\"content\":\"answer\"},\"finish_reason\":\"stop\"}]}\n\n"
			want := errors.New("sender")
			if err := d.ChatStreamlyWithSender(name, messages, api, cfg, func(*string, *string) error { return want }); !errors.Is(err, want) {
				t.Fatal(err)
			}
			ctx, cancel := context.WithCancel(t.Context())
			n := 0
			err = d.ChatStreamlyWithSender(name, messages, &APIConfig{ApiKey: &key, Context: ctx}, cfg, func(*string, *string) error { n++; cancel(); return nil })
			if !errors.Is(err, context.Canceled) || n != 1 {
				t.Fatal("continued after cancellation", n, err)
			}
			contentType = "application/json"
			for _, raw := range []string{`{"data":[]}`, `{"data":[{"id":"served"}]}`, `{}`, `{"data":null}`, `{"data":[{"id":" padded "}]}`, `{"data":[{"id":"same"},{"id":"same"}]}`, `{"data":[{"id":null}]}`, `{"error":"private-provider-body","data":[]}`} {
				response = raw
				valid := raw == `{"data":[]}` || raw == `{"data":[{"id":"served"}]}` || raw == `{"data":[{"id":" padded "}]}` || raw == `{"data":[{"id":"same"},{"id":"same"}]}` || raw == `{"data":[{"id":null}]}`
				_, err := d.ListModels(api)
				if valid != (err == nil) {
					t.Fatal(raw, err)
				}
			}
			before := calls.Load()
			f := false
			if err := d.ChatStreamlyWithSender(name, messages, api, &ChatConfig{Stream: &f}, func(*string, *string) error { return nil }); err == nil {
				t.Fatal("stream false")
			}
			if _, err := d.ListModels(&APIConfig{}); err == nil {
				t.Fatal("missing key")
			}
			region := "missing"
			if _, err := d.ListModels(&APIConfig{ApiKey: &key, Region: &region}); err == nil {
				t.Fatal("missing region")
			}
			if calls.Load() != before {
				t.Fatal("invalid call reached HTTP")
			}

			if _, err := d.Embed(&name, []string{"a"}, api, nil); err == nil {
				t.Fatal("unsupported embedding succeeded")
			}
		})
	}
}

func TestAfter775ASRWireAndFailure(t *testing.T) {
	for _, provider := range []string{"TogetherAI", "ZHIPU-AI", "OpenRouter"} {
		t.Run(provider, func(t *testing.T) {
			response := `{"text":""}`
			status := 200
			strictOptions := true
			var requests atomic.Int32
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				requests.Add(1)
				if r.URL.Path != "/asr" || r.Header.Get("Authorization") != "Bearer key" {
					t.Error("endpoint/auth lost")
				}
				if provider == "OpenRouter" {
					var b map[string]any
					if err := json.NewDecoder(r.Body).Decode(&b); err != nil {
						t.Error(err)
					}
					a := b["input_audio"].(map[string]any)
					if a["data"] != base64.StdEncoding.EncodeToString([]byte("audio")) || a["format"] != "wav" || b["model"] != "served" || strictOptions && (b["language"] != "en" || b["temperature"] != float64(0)) {
						t.Error("json ASR contract", b)
					}
				} else {
					if err := r.ParseMultipartForm(1024); err != nil {
						t.Error(err)
						return
					}
					defer r.MultipartForm.RemoveAll()
					if r.FormValue("model") != "served" || len(r.MultipartForm.Value["model"]) != 1 || r.FormValue("temperature") != "0" || r.FormValue("prompt") != "prior" {
						t.Error("multipart contract", r.MultipartForm.Value)
					}
					file, header, err := r.FormFile("file")
					if err != nil {
						t.Error(err)
						return
					}
					defer file.Close()
					raw, _ := io.ReadAll(file)
					if string(raw) != "audio" || !strings.HasSuffix(header.Filename, ".wav") {
						t.Error("audio bytes/format lost")
					}
					if provider == "ZHIPU-AI" && (len(r.MultipartForm.Value["hotwords"]) != 2 || r.FormValue("stream") != "false") {
						t.Error("repeated hotwords/stream lost", r.MultipartForm.Value)
					}
				}
				w.WriteHeader(status)
				fmt.Fprint(w, response)
			}))
			defer srv.Close()
			d := after775Driver(t, provider, srv.URL).(ModelDriver)
			name, key := "served", "key"
			api := &APIConfig{ApiKey: &key}
			cfg := &ASRConfig{Params: map[string]any{"temperature": 0, "hotwords": []string{"one", "two"}, "response_format": "json", "language": "en", "prompt": "prior"}}
			if r, err := d.TranscribeAudio(&name, modelFixtureFile(t, []byte("audio")), api, cfg); err != nil || r.Text != "" {
				t.Fatal("valid empty transcript", r, err)
			}
			for _, raw := range []string{`{}`, `{"text":null}`, `{"text":5}`, `{"text":"bad","error":"private-provider-body"}`, `{"text":"bad","code":9}`, `{"text":"ok"} trailing`} {
				response = raw
				if _, err := d.TranscribeAudio(&name, modelFixtureFile(t, []byte("audio")), api, cfg); err == nil || strings.Contains(err.Error(), "private-provider-body") {
					t.Fatal(raw, err)
				}
			}
			status = 401
			response = `{"error":"private-provider-body"}`
			if _, err := d.TranscribeAudio(&name, modelFixtureFile(t, []byte("audio")), api, cfg); err == nil || strings.Contains(err.Error(), "private-provider-body") {
				t.Fatal(err)
			}
			if _, err := d.TranscribeAudio(nil, modelFixtureFile(t, []byte("audio")), api, cfg); err == nil {
				t.Fatal("nil model")
			}
			if _, err := d.TranscribeAudio(&name, nil, api, cfg); err == nil {
				t.Fatal("nil audio")
			}
			status = 200
			response = `{"text":""}`
			strictOptions = false
			for _, params := range []map[string]any{{"model": "bad"}, {"stream": true}, {"file": "bad"}} {
				before := requests.Load()
				result, err := d.TranscribeAudio(&name, modelFixtureFile(t, []byte("audio")), api, &ASRConfig{Params: params})
				if provider == "OpenRouter" {
					if err != nil || result == nil || result.Text != "" || requests.Load() != before+1 {
						t.Fatal("canonical JSON options changed model binding", result, err)
					}
				} else if err == nil || result != nil || requests.Load() != before {
					t.Fatal("multipart identity replacement reached provider", err)
				}
			}

		})
	}
}

func TestAfter775TogetherAIRerankStrict(t *testing.T) {
	name, key := "served", "key"
	response := `{"results":[{"index":1,"relevance_score":0},{"index":0,"relevance_score":0.9}]}`
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, response) }))
	defer srv.Close()
	d := after775Driver(t, "TogetherAI", srv.URL)
	api := &APIConfig{ApiKey: &key}
	if r, err := d.Rerank(&name, "q", []string{"a", "b"}, api, nil); err != nil || len(r.Data) != 2 || r.Data[0].RelevanceScore != 0 {
		t.Fatal(r, err)
	}
	for _, raw := range []string{`{}`, `{"results":[]}`, `{"results":[{"index":0,"relevance_score":null},{"index":1,"relevance_score":0}]}`, `{"results":[{"index":0,"relevance_score":0},{"index":0,"relevance_score":0}]}`, `{"results":[{"index":0,"relevance_score":0},{"index":2,"relevance_score":0}]}`, `{"results":[{"index":0,"relevance_score":0},{"index":1,"relevance_score":1e999}]}`} {
		response = raw
		if _, err := d.Rerank(&name, "q", []string{"a", "b"}, api, nil); err == nil {
			t.Fatal(raw)
		}
	}
}

func TestAfter775SpeechAndStreams(t *testing.T) {
	for _, provider := range []string{"TogetherAI", "ZHIPU-AI"} {
		t.Run(provider, func(t *testing.T) {
			response := "audio"
			mime := "audio/wav"
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var b map[string]any
				if err := json.NewDecoder(r.Body).Decode(&b); err != nil {
					t.Error(err)
				}
				if b["model"] != "served" || b["input"] != "hello" || b["response_format"] != "wav" || b["voice"] != "voice" || b["speed"] != float64(0) {
					t.Error("speech input lost", b)
				}
				w.Header().Set("Content-Type", mime)
				fmt.Fprint(w, response)
			}))
			defer srv.Close()
			d := after775Driver(t, provider, srv.URL)
			tts := d.(ModelDriver)
			stream := d.(ModelDriver)
			name, key := "served", "key"
			api := &APIConfig{ApiKey: &key}
			cfg := &TTSConfig{Format: "wav", Params: map[string]any{"speed": 0, "voice": "voice"}}
			if r, err := tts.AudioSpeech(&name, stringPtr("hello"), api, cfg); err != nil || string(r.Audio) != "audio" {
				t.Fatal(r, err)
			}
			for _, typ := range []string{"application/json", "audio/mpeg"} {
				mime = typ
				if _, err := tts.AudioSpeech(&name, stringPtr("hello"), api, cfg); err == nil {
					t.Fatal("non audio/mismatch accepted")
				}
			}
			mime = "audio/wav"
			response = ""
			if _, err := tts.AudioSpeech(&name, stringPtr("hello"), api, cfg); err == nil {
				t.Fatal("empty audio accepted")
			}
			if provider == "TogetherAI" {
				mime = "text/event-stream"
				response = "data: {\"type\":\"conversation.item.audio_output.delta\",\"delta\":\"YXVkaW8=\"}\n\ndata: [DONE]\n\n"
			} else {
				response = "audio"
			}
			var audio []byte
			if err := stream.AudioSpeechWithSender(&name, stringPtr("hello"), api, cfg, func(value, _ *string) error {
				r := &TTSResponse{Audio: modelStreamBytesForTest(value)}
				audio = append(audio, r.Audio...)
				return nil
			}); err != nil || string(audio) != "audio" {
				t.Fatal(string(audio), err)
			}
			want := errors.New("sender")
			if err := stream.AudioSpeechWithSender(&name, stringPtr("hello"), api, cfg, func(value, _ *string) error { return want }); !errors.Is(err, want) {
				t.Fatal(err)
			}
			ctx, cancel := context.WithCancel(t.Context())
			n := 0
			err := stream.AudioSpeechWithSender(&name, stringPtr("hello"), &APIConfig{ApiKey: &key, Context: ctx}, cfg, func(value, _ *string) error { n++; cancel(); return nil })
			if !errors.Is(err, context.Canceled) || n != 1 {
				t.Fatal(n, err)
			}
			if provider == "TogetherAI" {
				for _, raw := range []string{"data: [DONE]\n\n", "data: {oops}\n\n", "data: {\"type\":\"conversation.item.audio_output.delta\",\"delta\":\"%%%\"}\n\n", "data: {\"error\":\"private-provider-body\"}\n\n", "data: {\"type\":\"conversation.item.audio_output.delta\",\"delta\":\"YQ==\"}\n\n", "data: [DONE]"} {
					response = raw
					if err := stream.AudioSpeechWithSender(&name, stringPtr("hello"), api, cfg, func(value, _ *string) error { return nil }); err == nil || strings.Contains(err.Error(), "private-provider-body") {
						t.Fatal(raw, err)
					}
				}
			}
		})
	}
}

func TestAfter775MediaDeadline(t *testing.T) {
	for _, provider := range []string{"TogetherAI", "ZHIPU-AI", "OpenRouter"} {
		t.Run(provider, func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.Copy(io.Discard, r.Body); <-r.Context().Done() }))
			defer srv.Close()
			name, key := "served", "key"
			ctx, cancel := context.WithTimeout(t.Context(), 30*time.Millisecond)
			defer cancel()
			_, err := after775Driver(t, provider, srv.URL).(ModelDriver).TranscribeAudio(&name, modelFixtureFile(t, []byte("audio")), &APIConfig{ApiKey: &key, Context: ctx}, nil)
			if !errors.Is(err, context.DeadlineExceeded) {
				t.Fatal("deadline lost", err)
			}
		})
	}
}
