package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
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
			api := &APIConfig{APIKey: &key}
			name := "m"
			if _, err := d.ChatWithMessages(name, api, []Message{{Role: "user", Content: "q"}}, nil); err != nil {
				t.Fatal(err)
			}
			if err := d.ChatStreamlyWithMessages(name, []Message{{Role: "user", Content: "q"}}, api, nil, func(*string, *string) error { return nil }); err != nil {
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
	if err := d.CheckConnection(&APIConfig{APIKey: &key}); err != nil {
		t.Fatal(err)
	}
	for _, build := range []func(string, []Message, *ChatConfig, bool) (map[string]any, error){moonshotChatBody, minimaxChatBody} {
		b, err := build(" m ", []Message{{Role: "user", Content: "q"}}, nil, false)
		if err != nil || b["model"] != "m" {
			t.Fatal(b, err)
		}
	}
}

func TestAfter7ddXiaomiMedia(t *testing.T) {
	name, key := " mimo ", " secret "
	api := &APIConfig{APIKey: &key}
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
					if b["model"] != "mimo" || b["stream"] != stream {
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
						if input != "data:audio/mpeg;base64,YWJj" {
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
				asr := &ASRConfig{Format: "mp3", Language: []string{"en"}}
				speech := &TTSConfig{Voice: "voice"}
				calls := 0
				if tts {
					if stream {
						if err := d.AudioSpeechWithSender(&name, "say", api, speech, func(r *TTSResponse) error {
							calls++
							if string(r.Audio) != "abc" {
								t.Error(r)
							}
							return nil
						}); err != nil {
							t.Fatal(err)
						}
					} else {
						r, err := d.AudioSpeech(&name, "say", api, speech)
						if err != nil || string(r.Audio) != "abc" {
							t.Fatal(r, err)
						}
					}
				} else {
					if stream {
						if err := d.TranscribeAudioWithSender(&name, []byte("abc"), api, asr, func(r *ASRResponse) error {
							calls++
							if *r.Text != "words" {
								t.Error(r)
							}
							return nil
						}); err != nil {
							t.Fatal(err)
						}
					} else {
						r, err := d.TranscribeAudio(&name, []byte("abc"), api, asr)
						if err != nil || *r.Text != "words" {
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
}

func TestAfter7ddXiaomiMediaFailures(t *testing.T) {
	name, key := "mimo", "key"
	api := &APIConfig{APIKey: &key}
	for _, raw := range []string{`data: garbage` + "\n\n", `data: {"choices":[{"delta":{"audio":{"data":"%%%"}}}]}` + "\n\ndata: [DONE]\n\n", `data: {"choices":[{"delta":{"audio":{"data":"YWJj"}}}]}` + "\n\n", `data: [DONE]` + "\n\n"} {
		d := xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/event-stream")
			fmt.Fprint(w, raw)
		})
		if err := d.AudioSpeechWithSender(&name, "text", api, nil, func(*TTSResponse) error { return nil }); err == nil {
			t.Fatal("accepted bad stream", raw)
		}
	}
	sentinel := errors.New("send failed")
	d := xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"text\"}}]}\n\ndata: [DONE]\n\n")
	})
	if err := d.TranscribeAudioWithSender(&name, []byte("abc"), api, nil, func(*ASRResponse) error { return sentinel }); !errors.Is(err, sentinel) {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	local := *api
	local.Context = ctx
	if _, err := d.TranscribeAudio(&name, []byte("abc"), &local, nil); err == nil {
		t.Fatal("cancel accepted")
	}
}

func TestAfter7ddXiaomiMediaFallbackAndDenials(t *testing.T) {
	name, key := "mimo", "key"
	api := &APIConfig{APIKey: &key}
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
			err = d.AudioSpeechWithSender(&name, "text", api, nil, func(*TTSResponse) error { calls++; return sentinel })
		} else {
			err = d.TranscribeAudioWithSender(&name, []byte("abc"), api, nil, func(*ASRResponse) error { calls++; return sentinel })
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
		func() error { _, err := d.TranscribeAudio(&name, []byte("abc"), api, nil); return err },
		func() error { _, err := d.AudioSpeech(&name, "text", api, nil); return err },
	} {
		err := run()
		if err == nil || strings.Contains(err.Error(), "private") {
			t.Fatal(err)
		}
	}
	before := calls
	for _, run := range []func() error{
		func() error { _, err := d.TranscribeAudio(nil, []byte("abc"), api, nil); return err },
		func() error { _, err := d.TranscribeAudio(&name, nil, api, nil); return err },
		func() error {
			_, err := d.TranscribeAudio(&name, []byte("abc"), api, &ASRConfig{Params: map[string]any{"mime": "text/plain"}})
			return err
		},
		func() error {
			_, err := d.TranscribeAudio(&name, []byte("abc"), api, &ASRConfig{Language: []string{"en", "zh"}})
			return err
		},
		func() error {
			_, err := d.TranscribeAudio(&name, []byte("abc"), api, &ASRConfig{Params: map[string]any{"messages": []any{}}})
			return err
		},
		func() error { _, err := d.AudioSpeech(&name, "", api, nil); return err },
		func() error { _, err := d.AudioSpeech(&name, "text", api, &TTSConfig{Voice: "bad\nvoice"}); return err },
		func() error { _, err := d.AudioSpeech(&name, "text", api, &TTSConfig{Format: "bogus"}); return err },
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
	api := &APIConfig{APIKey: &key}
	calls := 0
	d := xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"audio\":{\"data\":\"YWJj\"}}}]}\n\ndata: invalid\n\n")
	})
	if err := d.AudioSpeechWithSender(&name, "text", api, nil, func(*TTSResponse) error { calls++; return nil }); err == nil || calls != 1 {
		t.Fatal(err, calls)
	}
	d = xiaomiFixture(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(200)
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	})
	d.streamIdleTimeout = 10 * time.Millisecond
	if err := d.AudioSpeechWithSender(&name, "text", api, nil, func(*TTSResponse) error { return nil }); err == nil {
		t.Fatal("idle stream succeeded")
	}
}
