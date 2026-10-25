package models

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestAfterf9ASRMultipartAndFormatsHTTP(t *testing.T) {
	for _, provider := range []string{"OpenAI", "Groq"} {
		for _, format := range []string{"json", "verbose_json", "text", "srt", "vtt"} {
			t.Run(provider+format, func(t *testing.T) {
				srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if r.URL.Path != "/asr" || r.Header.Get("Authorization") != "Bearer key" || r.Header.Get("Accept") != "application/json" {
						t.Error("wrong ASR endpoint/auth/accept")
					}
					reader, err := r.MultipartReader()
					if err != nil {
						t.Error(err)
						return
					}
					fields := map[string][]string{}
					seenFile := false
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
						if part.FormName() == "file" {
							seenFile = true
							if string(raw) != "audio" || part.FileName() != "audio.mp3" {
								t.Error("bad byte upload")
							}
						} else {
							if seenFile {
								t.Error("options after file")
							}
							fields[part.FormName()] = append(fields[part.FormName()], string(raw))
						}
					}
					if fields["model"][0] != "whisper" || fields["language"][0] != "en" || fields["prompt"][0] != "hello" || fields["temperature"][0] != "0" || fields["response_format"][0] != format || !reflect.DeepEqual(fields["timestamp_granularities[]"], []string{"word", "segment"}) {
						t.Error(fields)
					}
					if format == "text" || format == "srt" || format == "vtt" {
						w.Header().Set("Content-Type", "text/plain")
						fmt.Fprint(w, "transcribed")
						return
					}
					w.Header().Set("Content-Type", "application/json")
					fmt.Fprint(w, `{"text":"transcribed"}`)
				}))
				defer srv.Close()
				cfg := &ASRConfig{Format: "mp3", Language: []string{"en"}, Prompt: "hello", Params: map[string]any{"response_format": format, "temperature": 0, "timestamp_granularities[]": []string{"word", "segment"}}}
				original, _ := json.Marshal(cfg)
				result, err := afterf9Driver(t, provider, srv.URL).(ASRDriver).TranscribeAudio(stringPtr("whisper"), []byte("audio"), &APIConfig{APIKey: stringPtr("key")}, cfg)
				if err != nil || result == nil || *result.Text != "transcribed" {
					t.Fatal(result, err)
				}
				current, _ := json.Marshal(cfg)
				if !bytes.Equal(original, current) {
					t.Fatal("ASR config mutated")
				}
			})
		}
	}
}

func TestAfterf9ASRInvalidHTTP(t *testing.T) {
	for _, provider := range []string{"OpenAI", "Groq"} {
		for _, body := range []string{"", `null`, `{}`, `{"text":null}`, `{"text":5}`, `{"text":"ok","error":"private-secret"}`, `{"text":"ok"} {}`} {
			t.Run(provider+body, func(t *testing.T) {
				srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) }))
				defer srv.Close()
				result, err := afterf9Driver(t, provider, srv.URL).(ASRDriver).TranscribeAudio(stringPtr("whisper"), []byte("audio"), &APIConfig{APIKey: stringPtr("key")}, nil)
				if err == nil || result != nil || strings.Contains(err.Error(), "private-secret") {
					t.Fatal(result, err)
				}
			})
		}
	}
}

func TestAfterf9SpeechHTTP(t *testing.T) {
	for _, provider := range []string{"OpenAI", "Groq", "Astraflow"} {
		t.Run(provider, func(t *testing.T) {
			srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/speech" || r.Header.Get("Authorization") != "Bearer key" {
					t.Error("speech auth/path lost")
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				if body["model"] != "tts" || body["input"] != "hello" || body["voice"] != "voice" || body["speed"] != 1.25 || body["stream"] != nil {
					t.Error(body)
				}
				if provider == "Astraflow" {
					if body["format"] != "wav" || body["response_format"] != nil {
						t.Error(body)
					}
				} else if body["response_format"] != "wav" {
					t.Error(body)
				}
				w.Header().Set("Content-Type", "audio/wav")
				w.Write([]byte{0, 255, 1, 2})
			}))
			defer srv.Close()
			cfg := &TTSConfig{Voice: "voice", Format: "wav", Params: map[string]any{"speed": 1.25}}
			original, _ := json.Marshal(cfg)
			result, err := afterf9Driver(t, provider, srv.URL).(TTSDriver).AudioSpeech(stringPtr("tts"), "hello", &APIConfig{APIKey: stringPtr("key")}, cfg)
			if err != nil || result == nil || result.Format != "wav" || !bytes.Equal(result.Audio, []byte{0, 255, 1, 2}) {
				t.Fatal(result, err)
			}
			current, _ := json.Marshal(cfg)
			if !bytes.Equal(current, original) {
				t.Fatal("speech config mutated")
			}
		})
	}
}

func TestAfterf9SpeechInvalidHTTP(t *testing.T) {
	for _, provider := range []string{"OpenAI", "Groq", "Astraflow"} {
		for _, test := range []struct {
			body, mime string
			status     int
		}{{"", "audio/mpeg", 200}, {`{"error":"private-secret"}`, "application/json", 200}, {"bytes", "audio/wav", 200}, {"private-secret", "audio/mpeg", 503}} {
			t.Run(fmt.Sprint(provider, test), func(t *testing.T) {
				srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					w.Header().Set("Content-Type", test.mime)
					w.WriteHeader(test.status)
					fmt.Fprint(w, test.body)
				}))
				defer srv.Close()
				result, err := afterf9Driver(t, provider, srv.URL).(TTSDriver).AudioSpeech(stringPtr("tts"), "hello", &APIConfig{APIKey: stringPtr("key")}, &TTSConfig{Voice: "voice"})
				if err == nil || result != nil || strings.Contains(err.Error(), "private-secret") {
					t.Fatal(result, err)
				}
			})
		}
	}
}

func TestAfterf9OpenAIASRStreamHTTP(t *testing.T) {
	for _, body := range []string{
		"data: {\"type\":\"transcript.text.delta\",\"delta\":\"hello\"}\n\ndata: {\"type\":\"transcript.text.delta\",\"delta\":\" world\"}\n\ndata: {\"type\":\"transcript.text.done\",\"text\":\"hello world\"}\n\n",
		"data: {\"type\":\"transcript.text.segment\",\"text\":\"hello world\"}\n\ndata: [DONE]\n\n",
		"data: {\"type\":\"transcript.text.done\",\"text\":\"hello world\"}\n\n",
	} {
		t.Run(body, func(t *testing.T) {
			srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Accept") != "text/event-stream" {
					t.Error("missing SSE accept")
				}
				if err := r.ParseMultipartForm(1024); err != nil {
					t.Error(err)
					return
				}
				defer r.MultipartForm.RemoveAll()
				if r.FormValue("stream") != "true" {
					t.Error("stream flag lost")
				}
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprint(w, body)
			}))
			defer srv.Close()
			text := ""
			err := afterf9Driver(t, "OpenAI", srv.URL).(ASRStreamDriver).TranscribeAudioWithSender(stringPtr("asr"), []byte("audio"), &APIConfig{APIKey: stringPtr("key")}, nil, func(result *ASRResponse) error { text += *result.Text; return nil })
			if err != nil || text != "hello world" {
				t.Fatal(text, err)
			}
		})
	}
}

func TestAfterf9OpenAIStreamsRejectMissingTerminal(t *testing.T) {
	for _, kind := range []string{"asr", "tts"} {
		payloads := []string{"", "data: garbage\n\n", "data: {\"error\":\"private-secret\"}\n\n", "data: [DONE]\n\n"}
		if kind == "asr" {
			payloads = append(payloads, "data: {\"type\":\"transcript.text.delta\",\"delta\":\"partial\"}\n\n", "data: {\"type\":\"transcript.text.done\"}\n\n", "data: {\"type\":\"transcript.text.done\",\"text\":\"partial\"}\n")
		} else {
			payloads = append(payloads, "data: {\"type\":\"speech.audio.delta\",\"audio\":\"YQ==\"}\n\n", "data: {\"type\":\"speech.audio.done\"}\n\n", "data: {\"type\":\"speech.audio.delta\",\"audio\":\"%%\"}\n\ndata: {\"type\":\"speech.audio.done\"}\n\n")
		}
		for i, body := range payloads {
			t.Run(fmt.Sprintf("%s/%d", kind, i), func(t *testing.T) {
				srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					w.Header().Set("Content-Type", "text/event-stream")
					fmt.Fprint(w, body)
				}))
				defer srv.Close()
				d := afterf9Driver(t, "OpenAI", srv.URL)
				api := &APIConfig{APIKey: stringPtr("key")}
				var err error
				if kind == "asr" {
					err = d.(ASRStreamDriver).TranscribeAudioWithSender(stringPtr("asr"), []byte("audio"), api, nil, func(*ASRResponse) error { return nil })
				} else {
					err = d.(TTSStreamDriver).AudioSpeechWithSender(stringPtr("tts"), "hello", api, &TTSConfig{Voice: "voice", Params: map[string]any{"stream_format": "sse"}}, func(*TTSResponse) error { return nil })
				}
				if err == nil || strings.Contains(err.Error(), "private-secret") {
					t.Fatal(err)
				}
			})
		}
	}
}

func TestAfterf9OpenAISpeechStreamsHTTP(t *testing.T) {
	for _, mode := range []string{"audio", "sse"} {
		t.Run(mode, func(t *testing.T) {
			expected := []byte{0, 255, 1, 2}
			srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				if body["stream_format"] != mode || body["stream"] != nil || body["voice"] != "voice" {
					t.Error(body)
				}
				if mode == "sse" {
					if r.Header.Get("Accept") != "text/event-stream" {
						t.Error("SSE accept lost")
					}
					w.Header().Set("Content-Type", "text/event-stream")
					fmt.Fprintf(w, "data: {\"type\":\"speech.audio.delta\",\"audio\":%q}\n\ndata: {\"type\":\"speech.audio.done\"}\n\n", base64.StdEncoding.EncodeToString(expected))
				} else {
					w.Header().Set("Content-Type", "audio/mpeg")
					w.Write(expected)
				}
			}))
			defer srv.Close()
			var output []byte
			err := afterf9Driver(t, "OpenAI", srv.URL).(TTSStreamDriver).AudioSpeechWithSender(stringPtr("tts"), "hello", &APIConfig{APIKey: stringPtr("key")}, &TTSConfig{Voice: "voice", Params: map[string]any{"stream_format": mode}}, func(result *TTSResponse) error { output = append(output, result.Audio...); return nil })
			if err != nil || !bytes.Equal(output, expected) {
				t.Fatal(output, err)
			}
		})
	}
}

func TestAfterf9OpenAISenderFailureAndCancel(t *testing.T) {
	for _, kind := range []string{"asr", "tts-sse", "tts-binary"} {
		for _, cancelInSender := range []bool{false, true} {
			t.Run(fmt.Sprint(kind, cancelInSender), func(t *testing.T) {
				stopped := make(chan struct{})
				srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					io.Copy(io.Discard, r.Body)
					if kind == "tts-binary" {
						w.Header().Set("Content-Type", "audio/mpeg")
						w.Write([]byte{0, 255, 1})
					} else {
						w.Header().Set("Content-Type", "text/event-stream")
						if kind == "asr" {
							fmt.Fprint(w, "data: {\"type\":\"transcript.text.delta\",\"delta\":\"hello\"}\n\n")
						} else {
							fmt.Fprint(w, "data: {\"type\":\"speech.audio.delta\",\"audio\":\"YQ==\"}\n\n")
						}
					}
					w.(http.Flusher).Flush()
					select {
					case <-r.Context().Done():
					case <-time.After(2 * time.Second):
						t.Error("upstream not canceled")
					}
					close(stopped)
				}))
				defer srv.Close()
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				sentinel := errors.New("sender failed")
				calls := 0
				sender := func() error {
					calls++
					if cancelInSender {
						cancel()
						return nil
					}
					return sentinel
				}
				d := afterf9Driver(t, "OpenAI", srv.URL)
				api := &APIConfig{APIKey: stringPtr("key"), Context: ctx}
				var err error
				if kind == "asr" {
					err = d.(ASRStreamDriver).TranscribeAudioWithSender(stringPtr("asr"), []byte("audio"), api, nil, func(*ASRResponse) error { return sender() })
				} else {
					mode := "sse"
					if kind == "tts-binary" {
						mode = "audio"
					}
					err = d.(TTSStreamDriver).AudioSpeechWithSender(stringPtr("tts"), "hello", api, &TTSConfig{Voice: "voice", Params: map[string]any{"stream_format": mode}}, func(*TTSResponse) error { return sender() })
				}
				expected := sentinel
				if cancelInSender {
					expected = context.Canceled
				}
				if !errors.Is(err, expected) || calls != 1 {
					t.Fatal(calls, err)
				}
				select {
				case <-stopped:
				case <-time.After(time.Second):
					t.Fatal("upstream leaked")
				}
			})
		}
	}
}

func TestAfterf9MediaPreflight(t *testing.T) {
	var calls atomic.Int32
	srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1) }))
	defer srv.Close()
	d := afterf9Driver(t, "OpenAI", srv.URL)
	api := &APIConfig{APIKey: stringPtr("key")}
	for _, cfg := range []*ASRConfig{{Params: map[string]any{"file": "elsewhere"}}, {Params: map[string]any{"response_format": 5}}, {Params: map[string]any{"response_format": "unknown"}}, {Language: []string{"en", "zh"}}} {
		if _, err := d.(ASRDriver).TranscribeAudio(stringPtr("asr"), []byte("audio"), api, cfg); err == nil {
			t.Fatal("invalid ASR config")
		}
	}
	for _, cfg := range []*TTSConfig{nil, {Params: map[string]any{"voice": 5}}, {Voice: "voice", Params: map[string]any{"input": "other"}}, {Voice: "voice", Params: map[string]any{"stream_format": "wrong"}}} {
		if _, err := d.(TTSDriver).AudioSpeech(stringPtr("tts"), "hello", api, cfg); err == nil {
			t.Fatal("invalid TTS config")
		}
	}
	if err := d.(ASRStreamDriver).TranscribeAudioWithSender(stringPtr("asr"), []byte("audio"), api, nil, nil); err == nil {
		t.Fatal("nil sender")
	}
	if err := d.(TTSStreamDriver).AudioSpeechWithSender(stringPtr("tts"), "hello", api, &TTSConfig{Voice: "voice"}, nil); err == nil {
		t.Fatal("nil sender")
	}
	if calls.Load() != 0 {
		t.Fatal("invalid media request reached provider")
	}
}

func TestAfterf9TextTranscriptionRejectsJSONError(t *testing.T) {
	srv := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		fmt.Fprint(w, `{"error":"private-secret"}`)
	}))
	defer srv.Close()
	for _, provider := range []string{"Groq", "OpenAI"} {
		result, err := afterf9Driver(t, provider, srv.URL).(ASRDriver).TranscribeAudio(stringPtr("whisper"), []byte("audio"), &APIConfig{APIKey: stringPtr("key")}, &ASRConfig{Params: map[string]any{"response_format": "text"}})
		if err == nil || result != nil || strings.Contains(err.Error(), "private-secret") {
			t.Fatal(result, err)
		}
	}
}
