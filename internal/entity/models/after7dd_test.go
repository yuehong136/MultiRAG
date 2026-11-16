package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"mime"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestAfter7ddGPUStackOptionalKey(t *testing.T) {
	for _, key := range []string{"", " secret ", " \t "} {
		t.Run(key, func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				expected := ""
				if strings.TrimSpace(key) != "" {
					expected = "Bearer secret"
				}
				if r.Header.Get("Authorization") != expected {
					t.Errorf("auth %q", r.Header.Get("Authorization"))
				}
				switch r.URL.Path {
				case "/models":
					fmt.Fprint(w, `{"data":[{"id":"m"}]}`)
				case "/embed":
					fmt.Fprint(w, `{"data":[{"index":0,"embedding":[1]}]}`)
				default:
					var b map[string]any
					json.NewDecoder(r.Body).Decode(&b)
					if b["stream"] == true {
						w.Header().Set("Content-Type", "text/event-stream")
						fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"ok\"},\"finish_reason\":null}]}\n\ndata: [DONE]\n\n")
					} else {
						fmt.Fprint(w, `{"choices":[{"message":{"content":"ok"}}]}`)
					}
				}
			}))
			d := NewGPUStackModel(map[string]string{"default": srv.URL}, URLSuffix{Chat: "chat", Models: "models", Embedding: "embed"})
			api := &APIConfig{ApiKey: &key}
			name := "m"
			if _, err := d.ChatWithMessages(name, []Message{{Role: "user", Content: "q"}}, api, nil); err != nil {
				t.Fatal(err)
			}
			if err := d.ChatStreamlyWithSender(name, []Message{{Role: "user", Content: "q"}}, api, nil, func(*string, *string) error { return nil }); err != nil {
				t.Fatal(err)
			}
			if _, err := d.ListModels(api); err != nil {
				t.Fatal(err)
			}
			if _, err := d.Embed(&name, []string{"q"}, api, nil); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestAfter7ddMiniMaxProbeAndTrim(t *testing.T) {
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" || r.URL.Path != "/models" {
			t.Errorf("probe %s %s", r.Method, r.URL.Path)
		}
		fmt.Fprint(w, `{"data":[{"id":"m"}]}`)
	}))
	d := NewMinimaxModel(map[string]string{"default": srv.URL}, URLSuffix{Models: "models", Files: "files"})
	key := "key"
	if err := d.CheckConnection(&APIConfig{ApiKey: &key}); err != nil {
		t.Fatal(err)
	}
	for _, build := range []func(string, []Message, *ChatConfig, bool) (map[string]any, error){func(name string, messages []Message,
		cfg *ChatConfig, stream bool) (map[string]any, error) {
		return captureChatBodyForTest(t, "Moonshot", name, messages, cfg, stream)
	}, func(name string, messages []Message,
		cfg *ChatConfig, stream bool) (map[string]any, error) {
		return captureChatBodyForTest(t, "MiniMax", name, messages, cfg, stream)
	},
	} {
		b, err := build(" m ", []Message{{Role: "user", Content: "q"}}, nil, false)
		if err != nil || b["model"] != "m" {
			t.Fatal(b, err)
		}
	}
}

func TestAfter7ddXiaomiMedia(t *testing.T) {
	name, key := "mimo", " secret "
	api := &APIConfig{ApiKey: &key}
	inputMIME := mime.TypeByExtension(".wav")
	if inputMIME == "" {
		inputMIME = http.DetectContentType([]byte("abc"))
	}
	for _, tts := range []bool{false, true} {
		for _, stream := range []bool{false, true} {
			t.Run(fmt.Sprint(tts, stream), func(t *testing.T) {
				d := xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) {
					w.Header().Set("Content-Type", "application/json")
					if r.Header.Get("api-key") != "secret" {
						t.Error("auth")
					}
					var b map[string]any
					if err := json.NewDecoder(r.Body).Decode(&b); err != nil {
						t.Fatal(err)
					}
					if b["model"] != "mimo" || (b["stream"] == true) != stream {
						t.Error(b)
					}
					messages := b["messages"].([]any)
					m := messages[0].(map[string]any)
					payload := `{"content":"words"}`
					if tts {
						if m["role"] != "assistant" || m["content"] != "say" {
							t.Error(b)
						}
						if b["audio"].(map[string]any)["voice"] != "voice" {
							t.Error(b)
						}
						payload = `{"audio":{"data":"YWJj"}}`
					} else {
						if b["asr_options"].(map[string]any)["language"] != "en" {
							t.Error(b)
						}
						input := m["content"].([]any)[0].(map[string]any)["input_audio"].(map[string]any)["data"].(string)
						if input != fmt.Sprintf("data:%s;base64,YWJj", inputMIME) {
							t.Error(input)
						}
					}
					if stream {
						w.Header().Set("Content-Type", "text/event-stream")
						fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":%s}]}\n\ndata: [DONE]\n\n", payload)
					} else {
						fmt.Fprintf(w, `{"choices":[{"message":%s}]}`, payload)
					}
				})
				asr := &ASRConfig{Params: map[string]any{"response_format": "mp3", "language": "en"}}
				speech := &TTSConfig{Params: map[string]any{"voice": "voice"}}
				calls := 0
				if tts {
					if stream {
						if err := d.AudioSpeechWithSender(&name, stringPtr("say"), api, speech, func(value, _ *string) error {
							raw, err := decodeXiaomiAudioData(*value)
							if err != nil {
								t.Fatal(err)
							}
							r := &TTSResponse{Audio: raw}
							calls++
							if string(r.Audio) != "abc" {
								t.Error(r)
							}
							return nil
						}); err != nil {
							t.Fatal(err)
						}
					} else {
						r, err := d.AudioSpeech(&name, stringPtr("say"), api, speech)
						if err != nil || string(r.Audio) != "abc" {
							t.Fatal(r, err)
						}
					}
				} else {
					if stream {
						if err := d.TranscribeAudioWithSender(&name, modelFixtureFile(t, []byte("abc")), api, asr, func(value, _ *string) error {
							if value == nil || *value == "[DONE]" {
								return nil
							}
							r := &ASRResponse{Text: *value}
							calls++
							if r.Text != "words" {
								t.Error(r)
							}
							return nil
						}); err != nil {
							t.Fatal(err)
						}
					} else {
						r, err := d.TranscribeAudio(&name, modelFixtureFile(t, []byte("abc")), api, asr)
						if err != nil || r.Text != "words" {
							t.Fatal(r, err)
						}
					}
				}
				if stream && calls != 1 {
					t.Fatal(calls)
				}
			})
		}
	}
	for _, stream := range []bool{false, true} {
		t.Run(fmt.Sprintf("mime_override_%t", stream), func(t *testing.T) {
			d := xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("api-key") != "secret" {
					t.Error("auth")
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Fatal(err)
				}
				if body["model"] != "mimo" || (body["stream"] == true) != stream || body["asr_options"].(map[string]any)["language"] != "fr" || body["mime_type"] != nil || body["mime"] != nil {
					t.Error(body)
				}
				message := body["messages"].([]any)[0].(map[string]any)
				input := message["content"].([]any)[0].(map[string]any)
				if message["role"] != "user" || input["type"] != "input_audio" || input["input_audio"].(map[string]any)["data"] != "data:audio/flac;base64,YWJj" {
					t.Error(body)
				}
				if stream {
					w.Header().Set("Content-Type", "text/event-stream")
					fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"words\"}}]}\n\ndata: [DONE]\n\n")
				} else {
					w.Header().Set("Content-Type", "application/json")
					fmt.Fprint(w, `{"choices":[{"message":{"content":"words"}}]}`)
				}
			})
			asr := &ASRConfig{Params: map[string]any{"mime_type": " audio/flac ", "mime": "audio/ogg", "language": "fr"}}
			if stream {
				calls := 0
				if err := d.TranscribeAudioWithSender(&name, modelFixtureFile(t, []byte("abc")), api, asr, func(value, _ *string) error {
					if value != nil && *value != "[DONE]" {
						calls++
						if *value != "words" {
							t.Error(*value)
						}
					}
					return nil
				}); err != nil || calls != 1 {
					t.Fatal(err, calls)
				}
			} else {
				response, err := d.TranscribeAudio(&name, modelFixtureFile(t, []byte("abc")), api, asr)
				if err != nil || response == nil || response.Text != "words" {
					t.Fatal(response, err)
				}
			}
		})
	}
}

func TestAfter7ddXiaomiMediaFailures(t *testing.T) {
	name, key := "mimo", "key"
	api := &APIConfig{ApiKey: &key}
	for _, raw := range []string{`data: garbage` + "\n\n", `data: {"choices":[{"delta":{"audio":{"data":"%%%"}}}]}` + "\n\ndata: [DONE]\n\n", `data: {"choices":[{"delta":{"audio":{"data":"YWJj"}}}]}` + "\n\n", `data: [DONE]` + "\n\n"} {
		d := xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/event-stream")
			fmt.Fprint(w, raw)
		})
		if err := d.AudioSpeechWithSender(&name, stringPtr("text"), api, nil, func(value, _ *string) error { return nil }); err == nil {
			t.Fatal("accepted bad stream", raw)
		}
	}
	sentinel := errors.New("send failed")
	d := xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"text\"}}]}\n\ndata: [DONE]\n\n")
	})
	if err := d.TranscribeAudioWithSender(&name, modelFixtureFile(t, []byte("abc")), api, nil, func(value, _ *string) error { return sentinel }); !errors.Is(err, sentinel) {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	local := *api
	local.Context = ctx
	if _, err := d.TranscribeAudio(&name, modelFixtureFile(t, []byte("abc")), &local, nil); err == nil {
		t.Fatal("cancel accepted")
	}
}

func TestAfter7ddXiaomiMediaFallbackAndDenials(t *testing.T) {
	name, key := "mimo", "key"
	api := &APIConfig{ApiKey: &key}
	for _, tts := range []bool{false, true} {
		d := xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			if tts {
				fmt.Fprint(w, `{"choices":[{"message":{"audio":{"data":"YWJj"}}}]}`)
			} else {
				fmt.Fprint(w, `{"choices":[{"message":{"content":"text"}}]}`)
			}
		})
		sentinel := errors.New("sink failed")
		calls := 0
		var err error
		if tts {
			err = d.AudioSpeechWithSender(&name, stringPtr("text"), api, nil, func(value, _ *string) error { calls++; return sentinel })
		} else {
			err = d.TranscribeAudioWithSender(&name, modelFixtureFile(t, []byte("abc")), api, nil, func(value, _ *string) error { calls++; return sentinel })
		}
		if !errors.Is(err, sentinel) || calls != 1 {
			t.Fatal(err, calls)
		}
	}
	calls := 0
	d := xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(401)
		fmt.Fprint(w, "private provider payload")
	})
	for _, run := range []func() error{
		func() error {
			_, err := d.TranscribeAudio(&name, modelFixtureFile(t, []byte("abc")), api, nil)
			return err
		},
		func() error { _, err := d.AudioSpeech(&name, stringPtr("text"), api, nil); return err },
	} {
		err := run()
		if err == nil || strings.Contains(err.Error(), "private") {
			t.Fatal(err)
		}
	}
	before := calls
	for _, run := range []func() error{
		func() error {
			_, err := d.TranscribeAudio(nil, modelFixtureFile(t, []byte("abc")), api, nil)
			return err
		},
		func() error { _, err := d.TranscribeAudio(&name, nil, api, nil); return err },

		func() error { _, err := d.AudioSpeech(&name, stringPtr(""), api, nil); return err },
	} {
		if err := run(); err == nil {
			t.Fatal("invalid input accepted")
		}
	}
	if calls != before {
		t.Fatal("invalid input sent HTTP", calls, before)
	}
}

func TestAfter7ddXiaomiMediaLaterErrorAndIdle(t *testing.T) {
	name, key := "mimo", "key"
	api := &APIConfig{ApiKey: &key}
	calls := 0
	d := xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"audio\":{\"data\":\"YWJj\"}}}]}\n\ndata: invalid\n\n")
	})
	if err := d.AudioSpeechWithSender(&name, stringPtr("text"), api, nil, func(value, _ *string) error { calls++; return nil }); err == nil || calls != 1 {
		t.Fatal(err, calls)
	}
	d = xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(200)
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	})
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Millisecond)
	defer cancel()
	api.Context = ctx
	if err := d.AudioSpeechWithSender(&name, stringPtr("text"), api, nil, func(value, _ *string) error { return nil }); err == nil {
		t.Fatal("idle stream succeeded")
	}
}

func TestAfter7ddXiaomiSpeechFormatContract(t *testing.T) {
	name, key := "mimo", "key"
	api := &APIConfig{ApiKey: &key}
	for _, cfg := range []*TTSConfig{nil, {Params: map[string]any{"voice": "voice"}}, {Format: "wav", Params: map[string]any{"format": "mp3"}}, {Params: map[string]any{"audio_setting": map[string]any{"format": "wav"}}}} {
		for _, stream := range []bool{false, true} {
			expected := "wav"
			if cfg != nil && cfg.Format != "" {
				expected = cfg.Format
			}
			if cfg != nil {
				if format, ok := cfg.Params["format"].(string); ok {
					expected = format
				}
			}
			var err error
			d := xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) {
				var body map[string]any
				json.NewDecoder(r.Body).Decode(&body)
				if body["audio"].(map[string]any)["format"] != expected {
					t.Error(body)
				}
				w.Header().Set("Content-Type", "application/json")
				fmt.Fprint(w, `{"choices":[{"message":{"audio":{"data":"YWJj"}}}]}`)
			})
			if stream {
				err = d.AudioSpeechWithSender(&name, stringPtr("text"), api, cfg, func(value, _ *string) error {
					if len(modelStreamBytesForTest(value)) == 0 {
						t.Error("empty audio")
					}
					return nil
				})
			} else {
				var result *TTSResponse
				result, err = d.AudioSpeech(&name, stringPtr("text"), api, cfg)
				if result == nil || len(result.Audio) == 0 {
					t.Error("empty audio")
				}
			}
			if err != nil {
				t.Fatal(err)
			}
		}
	}
}

func TestAfter7ddXiaomiRejectedFinishNeverDelivered(t *testing.T) {
	name, key := "mimo", "key"
	api := &APIConfig{ApiKey: &key}
	for _, stream := range []bool{false, true} {
		for _, tts := range []bool{false, true} {
			for _, reason := range []string{"content_filter", "error", "tool_calls"} {
				d := xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) {
					payload := `{"content":"partial"}`
					if tts {
						payload = `{"audio":{"data":"YWJj"}}`
					}
					if stream {
						w.Header().Set("Content-Type", "text/event-stream")
						fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":%s,\"finish_reason\":%q}]}\n\ndata: [DONE]\n\n", payload, reason)
					} else {
						w.Header().Set("Content-Type", "application/json")
						fmt.Fprintf(w, `{"choices":[{"message":%s,"finish_reason":%q}]}`, payload, reason)
					}
				})
				calls := 0
				var err error
				if tts {
					if stream {
						err = d.AudioSpeechWithSender(&name, stringPtr("text"), api, nil, func(value, _ *string) error { calls++; return nil })
					} else {
						_, err = d.AudioSpeech(&name, stringPtr("text"), api, nil)
					}
				} else {
					if stream {
						err = d.TranscribeAudioWithSender(&name, modelFixtureFile(t, []byte("abc")), api, nil, func(value, _ *string) error { calls++; return nil })
					} else {
						_, err = d.TranscribeAudio(&name, modelFixtureFile(t, []byte("abc")), api, nil)
					}
				}
				if err == nil || calls != 0 {
					t.Fatal(stream, tts, reason, err, calls)
				}
			}
		}
	}
}

func TestAfter7ddOllamaBlankKeyOmitsAuthorization(t *testing.T) {
	key := " \t "
	name := "model"
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, exists := r.Header["Authorization"]; exists {
			t.Errorf("blank authorization sent %q", r.Header.Get("Authorization"))
		}
		fmt.Fprint(w, `{"message":{"content":"ok"},"done":true}`)
	}))
	d := NewOllamaModel(map[string]string{"default": srv.URL}, URLSuffix{Chat: "chat"})
	if _, err := d.ChatWithMessages(name, []Message{{Role: "user", Content: "q"}}, &APIConfig{ApiKey: &key}, nil); err != nil {
		t.Fatal(err)
	}
}
