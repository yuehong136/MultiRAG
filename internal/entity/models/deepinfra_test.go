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
	"strings"
	"testing"
)

func TestDeepInfraEmbeddingsHTTP(t *testing.T) { testHostedEmbeddingsHTTP(t, "DeepInfra") }

func TestDeepInfraDiscoveryAndBalanceHTTP(t *testing.T) {
	for _, test := range []struct {
		path, body string
		valid      bool
	}{
		{"models/list", `[]`, true}, {"models/list", `[{"model_name":"chat"}]`, true},
		{"models/list", `null`, false}, {"models/list", `{}`, false}, {"models/list", `[{}]`, false}, {"models/list", `[{"model_name":"a"},{"model_name":"a"}]`, false}, {"models/list", `[] {}`, false},
		{"payment/checklist", `{"stripe_balance":0}`, true}, {"payment/checklist", `{"stripe_balance":1.5}`, true},
		{"payment/checklist", `{}`, false}, {"payment/checklist", `{"stripe_balance":null}`, false}, {"payment/checklist", `{"stripe_balance":-1}`, true}, {"payment/checklist", `{"stripe_balance":"0"}`, false}, {"payment/checklist", `{"stripe_balance":1e999}`, false}, {"payment/checklist", `{"stripe_balance":0,"error":"private-body"}`, false}, {"payment/checklist", `{"stripe_balance":0} {}`, false},
	} {
		t.Run(test.path+"/"+test.body, func(t *testing.T) {
			calls := 0
			fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				if r.Method != "GET" || r.URL.Path != "/office/"+test.path {
					t.Error("endpoint lost")
				}
				auth := ""
				if test.path == "payment/checklist" {
					auth = "Bearer key"
				}
				if r.Header.Get("Authorization") != auth {
					t.Error("credential policy")
				}
				fmt.Fprint(w, test.body)
			}))
			defer fixture.Close()
			d := NewDeepInfraModel(map[string]string{"office": fixture.URL + "/office/"}, URLSuffix{Models: "models/list", Balance: "payment/checklist"})
			api := &APIConfig{APIKey: stringPtr("key"), Region: stringPtr("office")}
			var err error
			if test.path == "models/list" {
				var names []string
				names, err = d.ListModels(api)
				if test.valid && names == nil || !test.valid && names != nil {
					t.Fatal(names, err)
				}
			} else {
				var balance map[string]any
				balance, err = d.Balance(api)
				if test.valid && (balance == nil || balance["currency"] != "USD") || !test.valid && balance != nil {
					t.Fatal(balance, err)
				}
				if err := d.CheckConnection(api); (err == nil) != test.valid || calls != 2 {
					t.Fatal("connection must check authenticated balance", err, calls)
				}
			}
			if (err == nil) != test.valid || err != nil && strings.Contains(err.Error(), "private-body") {
				t.Fatal(err)
			}
		})
	}
}

func TestDeepInfraThinkingBody(t *testing.T) {
	for _, thinking := range []*bool{nil, anthropicTestPtr(false), anthropicTestPtr(true)} {
		effort := "high"
		body, err := deepInfraChatBody("model", []Message{{Role: "user", Content: "q"}}, &ChatConfig{Thinking: thinking, Effort: &effort}, false)
		if err != nil {
			t.Fatal(err)
		}
		if thinking != nil && !*thinking {
			if body["reasoning_effort"] != nil || !reflect.DeepEqual(body["reasoning"], map[string]any{"enabled": false}) {
				t.Fatal(body)
			}
		} else if body["reasoning_effort"] != "high" {
			t.Fatal(body)
		}
	}
}

func TestDeepInfraMediaHTTP(t *testing.T) {
	audio := []byte{0, 255, 1, 128, 9}
	fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer key" {
			t.Error("media key")
		}
		if r.URL.Path == "/v1/audio/transcriptions" {
			reader, err := r.MultipartReader()
			if err != nil {
				t.Error(err)
				return
			}
			var names []string
			for {
				part, err := reader.NextPart()
				if err == io.EOF {
					break
				}
				if err != nil {
					t.Error(err)
					return
				}
				data, _ := io.ReadAll(part)
				names = append(names, part.FormName())
				switch part.FormName() {
				case "file":
					if !reflect.DeepEqual(data, audio) || part.FileName() != "audio.mp3" {
						t.Error("ASR bytes")
					}
				case "model":
					if string(data) != "asr" {
						t.Error("ASR name")
					}
				case "language":
					if string(data) != "en" {
						t.Error("ASR language")
					}
				}
			}
			if !reflect.DeepEqual(names, []string{"model", "language", "prompt", "temperature", "file"}) {
				t.Error("multipart order", names)
			}
			fmt.Fprint(w, `{"text":"transcription"}`)
			return
		}
		if r.URL.Path != "/v1/text-to-speech/voice_id" && r.URL.Path != "/v1/text-to-speech/voice_id/stream" {
			t.Error("speech path", r.URL.Path)
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body["text"] != "hello" || body["output_format"] != "wav" || body["model_id"] != "tts" || body["speed"] != float64(0) || body["voice"] != nil || body["voice_id"] != nil {
			t.Error("speech options", body)
		}
		w.Header().Set("Content-Type", "audio/wav")
		w.Write(audio)
	}))
	defer fixture.Close()
	d := NewDeepInfraModel(map[string]string{"default": fixture.URL}, URLSuffix{ASR: "v1/audio/transcriptions", TTS: "v1/text-to-speech"})
	api := &APIConfig{APIKey: stringPtr("key")}
	result, err := d.TranscribeAudio(stringPtr("asr"), audio, api, &ASRConfig{Format: "mp3", Language: []string{"en"}, Prompt: "context", Params: map[string]any{"temperature": 0}})
	if err != nil || result == nil || *result.Text != "transcription" {
		t.Fatal(result, err)
	}
	config := &TTSConfig{Voice: "voice_id", Format: "wav", Params: map[string]any{"speed": 0, "voice_id": "ignored"}}
	speech, err := d.AudioSpeech(stringPtr("tts"), "hello", api, config)
	if err != nil || speech == nil || !reflect.DeepEqual(speech.Audio, audio) || speech.Format != "wav" {
		t.Fatal(speech, err)
	}
	var output []byte
	err = d.AudioSpeechWithSender(stringPtr("tts"), "hello", api, config, func(chunk *TTSResponse) error { output = append(output, chunk.Audio...); return nil })
	if err != nil || !reflect.DeepEqual(output, audio) {
		t.Fatal(output, err)
	}
	marker := errors.New("sender failed")
	if err := d.AudioSpeechWithSender(stringPtr("tts"), "hello", api, config, func(*TTSResponse) error { return marker }); !errors.Is(err, marker) {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if result, err := d.AudioSpeech(stringPtr("tts"), "hello", &APIConfig{APIKey: stringPtr("key"), Context: ctx}, config); !errors.Is(err, context.Canceled) || result != nil {
		t.Fatal(result, err)
	}
}

func TestDeepInfraMediaRejectsFailuresHTTP(t *testing.T) {
	for _, test := range []struct {
		status     int
		mime, body string
	}{
		{401, "application/json", `{"error":"private-body"}`}, {500, "audio/wav", "a"}, {200, "application/json", `{"error":"private-body"}`}, {200, "audio/mpeg", "a"}, {200, "audio/wav", ""},
	} {
		t.Run(fmt.Sprint(test), func(t *testing.T) {
			fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", test.mime)
				w.WriteHeader(test.status)
				fmt.Fprint(w, test.body)
			}))
			defer fixture.Close()
			d := NewDeepInfraModel(map[string]string{"default": fixture.URL}, URLSuffix{TTS: "tts"})
			api := &APIConfig{APIKey: stringPtr("key")}
			config := &TTSConfig{Voice: "voice", Format: "wav"}
			result, err := d.AudioSpeech(stringPtr("tts"), "hello", api, config)
			if err == nil || result != nil || strings.Contains(err.Error(), "private-body") {
				t.Fatal(result, err)
			}
			if err := d.AudioSpeechWithSender(stringPtr("tts"), "hello", api, config, func(*TTSResponse) error { return nil }); err == nil {
				t.Fatal("bad stream succeeded")
			}
		})
	}
	for _, body := range []string{`{}`, `{"text":null}`, `{"error":"private-body"}`, `{"text":"ok","code":1}`, `{"text":42}`, `{"text":"ok"} {}`} {
		fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
		d := NewDeepInfraModel(map[string]string{"default": fixture.URL}, URLSuffix{ASR: "asr"})
		result, err := d.TranscribeAudio(stringPtr("asr"), []byte{1}, &APIConfig{APIKey: stringPtr("key")}, nil)
		fixture.Close()
		if err == nil || result != nil || strings.Contains(err.Error(), "private-body") {
			t.Fatal(result, err)
		}
	}
}

func TestDeepInfraMediaPreflight(t *testing.T) {
	d := NewDeepInfraModel(map[string]string{"default": "http://unused.invalid"}, URLSuffix{TTS: "tts"})
	calls := 0
	d.httpClient.Transport = lmStudioTransportFunc(func(r *http.Request) (*http.Response, error) { calls++; return nil, errors.New("unexpected request") })
	for _, config := range []*TTSConfig{nil, {}, {Voice: "../voice"}, {Voice: "voice", Params: map[string]any{"voice_id": 42}}, {Voice: "voice", Params: map[string]any{"output_format": "unknown"}}, {Voice: "voice", Params: map[string]any{"model": "wrong"}}} {
		if result, err := d.AudioSpeech(stringPtr("tts"), "hello", &APIConfig{APIKey: stringPtr("key")}, config); err == nil || result != nil {
			t.Fatal(result, err)
		}
	}
	if err := d.AudioSpeechWithSender(stringPtr("tts"), "hello", nil, nil, nil); err == nil {
		t.Fatal("missing sender")
	}
	if result, err := d.TranscribeAudio(stringPtr("asr"), nil, nil, nil); err == nil || result != nil {
		t.Fatal(result, err)
	}
	if calls != 0 {
		t.Fatal("invalid media reached provider")
	}
}

func TestDeepInfraEmbeddingMinimumDimension(t *testing.T) {
	var dimension int
	calls := 0
	fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		var body struct {
			Dimensions int
			Input      []string
		}
		json.NewDecoder(r.Body).Decode(&body)
		dimension = body.Dimensions
		data := []map[string]any{{"index": 0, "embedding": make([]float64, dimension)}}
		json.NewEncoder(w).Encode(map[string]any{"data": data})
	}))
	defer fixture.Close()
	d := NewDeepInfraModel(map[string]string{"default": fixture.URL}, URLSuffix{Embedding: "embeddings"})
	api := &APIConfig{APIKey: stringPtr("key")}
	for _, size := range []int{-1, 1, 31} {
		if result, err := d.Embed(stringPtr("model"), []string{"q"}, api, &EmbeddingConfig{Dimension: size}); err == nil || result != nil {
			t.Fatal(result, err)
		}
	}
	if calls != 0 {
		t.Fatal("invalid dimensions made HTTP request")
	}
	if result, err := d.Embed(stringPtr("model"), []string{"q"}, api, &EmbeddingConfig{Dimension: 32}); err != nil || len(result) != 1 || len(result[0].Embedding) != 32 || dimension != 32 || calls != 1 {
		t.Fatal(result, err, dimension, calls)
	}
}
