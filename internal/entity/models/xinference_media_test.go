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
	"testing"
	"time"
)

func TestXinferenceMediaKeylessKeyedAndOptions(t *testing.T) {
	for _, key := range []*string{nil, stringPtr("key")} {
		for _, suffix := range []string{"", "/v1/"} {
			t.Run(fmt.Sprint(key, suffix), func(t *testing.T) {
				audio := []byte{0, 1, 2, 255}
				calls := 0
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					calls++
					wantAuth := ""
					if key != nil {
						wantAuth = "Bearer " + *key
					}
					if r.Header.Get("Authorization") != wantAuth {
						t.Error("wrong optional auth")
					}
					switch r.URL.Path {
					case "/v1/audio/transcriptions":
						reader, err := r.MultipartReader()
						if err != nil {
							t.Error(err)
							return
						}
						fields := map[string]string{}
						last := ""
						for {
							part, err := reader.NextPart()
							if err == io.EOF {
								break
							}
							if err != nil {
								t.Error(err)
								return
							}
							raw, _ := io.ReadAll(part)
							last = part.FormName()
							if last == "file" {
								if !bytes.Equal(raw, audio) || part.FileName() != "audio.mp3" {
									t.Error("audio corrupted")
								}
							} else {
								fields[last] = string(raw)
							}
						}
						if last != "file" || fields["model"] != "whisper" || fields["temperature"] != "0" || fields["flag"] != "false" || fields["language"] != `["en","zh"]` || fields["prompt"] != "context" {
							t.Error(fields, last)
						}
						fmt.Fprint(w, `{"text":""}`)
					case "/v1/audio/speech":
						var body map[string]any
						if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
							t.Error(err)
						}
						if body["model"] != "voice" || body["input"] != "hello" || body["response_format"] != "wav" || body["format"] != nil || body["stream"] != false || body["speed"] != float64(0) || body["voice"] != "" {
							t.Error(body)
						}
						w.Header().Set("Content-Type", "audio/wav")
						w.Write(audio)
					default:
						t.Error("wrong suffix", r.URL.Path)
					}
				}))
				defer srv.Close()
				x := NewXinferenceModel(map[string]string{"default": srv.URL + suffix}, URLSuffix{ASR: "v1/audio/transcriptions", TTS: "v1/audio/speech"})
				api := &APIConfig{APIKey: key}
				asr := &ASRConfig{Format: "mp3", Prompt: "context", Language: []string{"en", "zh"}, Params: map[string]any{"temperature": 0, "flag": false}}
				before, _ := json.Marshal(asr)
				response, err := x.TranscribeAudio(stringPtr("whisper"), audio, api, asr)
				if err != nil || response.Text == nil || *response.Text != "" {
					t.Fatal(response, err)
				}
				after, _ := json.Marshal(asr)
				if !bytes.Equal(before, after) {
					t.Fatal("mutated ASR options")
				}
				config := &TTSConfig{Format: "wav", Params: map[string]any{"speed": 0, "voice": ""}}
				before, _ = json.Marshal(config)
				speech, err := x.AudioSpeech(stringPtr("voice"), "hello", api, config)
				if err != nil || speech.Format != "wav" || !bytes.Equal(speech.Audio, audio) || calls != 2 {
					t.Fatal(speech, err, calls)
				}
				after, _ = json.Marshal(config)
				if !bytes.Equal(before, after) {
					t.Fatal("mutated TTS options")
				}
				// Hosted media retains its required-key default.
				if _, err := multipartASR(x.httpClient, x.BaseURL, x.URLSuffix.ASR, stringPtr("whisper"), audio, nil, nil); err == nil || calls != 2 {
					t.Fatal("hosted key requirement relaxed")
				}
			})
		}
	}
}

func TestXinferenceMediaFailures(t *testing.T) {
	for _, test := range []struct {
		body, typ string
		status    int
		tts       bool
	}{
		{`{}`, "application/json", 200, false}, {`{"text":null}`, "application/json", 200, false}, {`{"text":42}`, "application/json", 200, false}, {`{"text":"ok","error":"private-secret"}`, "application/json", 200, false}, {`{"text":"ok","code":1}`, "application/json", 200, false},
		{"private-secret", "application/json", 401, false}, {strings.Repeat("x", MaxMediaBytes+MaxMediaParamsBytes+1), "application/json", 200, false},
		{`{"error":"private-secret"}`, "application/json", 200, true}, {"audio", "audio/mpeg", 200, true}, {"", "audio/wav", 200, true}, {"audio", "audio/wav", 500, true}, {strings.Repeat("x", MaxMediaBytes+1), "audio/wav", 200, true},
	} {
		t.Run(fmt.Sprint(test.tts, test.status, len(test.body)), func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", test.typ)
				w.WriteHeader(test.status)
				fmt.Fprint(w, test.body)
			}))
			defer srv.Close()
			x := NewXinferenceModel(map[string]string{"default": srv.URL}, URLSuffix{ASR: "asr", TTS: "tts"})
			var err error
			if test.tts {
				var result *TTSResponse
				result, err = x.AudioSpeech(stringPtr("voice"), "hello", nil, &TTSConfig{Format: "wav"})
				if result != nil {
					t.Error("partial speech")
				}
			} else {
				var result *ASRResponse
				result, err = x.TranscribeAudio(stringPtr("whisper"), []byte{1}, nil, nil)
				if result != nil {
					t.Error("partial transcription")
				}
			}
			if err == nil || strings.Contains(err.Error(), "private-secret") {
				t.Fatal(err)
			}
		})
	}
	x := NewXinferenceModel(map[string]string{"default": "http://127.0.0.1:1"}, URLSuffix{ASR: "asr", TTS: "tts"})
	for _, params := range []map[string]any{{"model": "override"}, {"stream": true}, {"input": "override"}} {
		if _, err := x.AudioSpeech(stringPtr("voice"), "hello", nil, &TTSConfig{Params: params}); err == nil {
			t.Fatal("identity override accepted")
		}
		if _, err := x.TranscribeAudio(stringPtr("whisper"), []byte{1}, nil, &ASRConfig{Params: params}); err == nil {
			t.Fatal("identity override accepted")
		}
	}
	if _, ok := any(x).(TTSStreamDriver); ok {
		t.Fatal("unsupported streaming advertised")
	}
}

func TestXinferenceMediaCancellation(t *testing.T) {
	for _, tts := range []bool{false, true} {
		t.Run(fmt.Sprint(tts), func(t *testing.T) {
			started, closed := make(chan struct{}), make(chan struct{})
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				io.Copy(io.Discard, r.Body)
				close(started)
				<-r.Context().Done()
				close(closed)
			}))
			defer srv.Close()
			x := NewXinferenceModel(map[string]string{"default": srv.URL}, URLSuffix{ASR: "asr", TTS: "tts"})
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			done := make(chan error, 1)
			go func() {
				var err error
				api := &APIConfig{Context: ctx}
				if tts {
					_, err = x.AudioSpeech(stringPtr("v"), "hello", api, nil)
				} else {
					_, err = x.TranscribeAudio(stringPtr("w"), []byte{1}, api, nil)
				}
				done <- err
			}()
			select {
			case <-started:
			case <-time.After(time.Second):
				t.Fatal("request not started")
			}
			cancel()
			if err := <-done; !errors.Is(err, context.Canceled) {
				t.Fatal(err)
			}
			select {
			case <-closed:
			case <-time.After(time.Second):
				t.Fatal("request leaked")
			}
		})
	}
}
