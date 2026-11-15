package models

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/hex"
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

func speechString(s string) *string { return &s }
func speechDriver(kind, base string) ModelDriver {
	urls := map[string]string{"default": base, "global": base}
	suffix := URLSuffix{TTS: "speech", ASR: "asr"}
	switch kind {
	case "minimax":
		return NewMinimaxModel(urls, suffix)
	case "fishaudio":
		return NewFishAudioModel(urls, suffix)
	default:
		return NewOpenRouterModel(urls, suffix)
	}
}
func miniSpeechFrame(audio string, status int) string {
	return fmt.Sprintf(`{"base_resp":{"status_code":0},"data":{"audio":%q,"status":%d}}`, hex.EncodeToString([]byte(audio)), status)
}
func speechSSE(raw string) string { return "data: " + raw + "\n\n" }
func TestSpeechProviderRequestsAndFormats(t *testing.T) {
	for _, kind := range []string{"minimax", "fishaudio", "openrouter"} {
		t.Run(kind, func(t *testing.T) {
			for _, explicit := range []bool{false, true} {
				t.Run(fmt.Sprint(explicit), func(t *testing.T) {
					format := "wav"
					if explicit {
						format = "mp3"
					}
					srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						if r.Method != "POST" || r.URL.Path != "/speech" || r.Header.Get("Authorization") != "Bearer fixture" || r.Header.Get("Content-Type") != "application/json" {
							t.Error("wrong request metadata")
						}
						var body map[string]any
						if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
							t.Error(err)
						}
						if body["temperature"] != 0.3 {
							t.Error("params dropped")
						}
						switch kind {
						case "minimax":
							if body["model"] != "audio/model" || body["text"] != "hello" || body["stream"] != false || body["output_format"] != nil {
								t.Error("frozen MiniMax request changed")
							}
							settings, _ := body["audio_setting"].(map[string]any)
							voice, _ := body["voice_setting"].(map[string]any)
							if settings["format"] != format || voice["speed"] != float64(1) || voice["voice_id"] != nil || body["voice"] != "voice" {
								t.Error("native settings/free parameters changed")
							}
							if !explicit && settings["sample_rate"] != float64(32000) {
								t.Error("native audio setting dropped")
							}
							io.WriteString(w, miniSpeechFrame("audio bytes", 2))
						case "fishaudio":
							if r.Header.Get("model") != "audio/model" || body["text"] != "hello" || body["format"] != format || body["voice"] != "voice" {
								t.Error("wrong fish body")
							}
							w.Header().Set("Content-Type", "audio/"+map[string]string{"wav": "wav", "mp3": "mpeg"}[format])
							io.WriteString(w, "audio bytes")
						default:
							if body["model"] != "audio/model" || body["input"] != "hello" || body["response_format"] != format || body["voice"] != "voice" {
								t.Error("wrong openrouter body")
							}
							w.Header().Set("Content-Type", "audio/"+map[string]string{"wav": "wav", "mp3": "mpeg"}[format])
							io.WriteString(w, "audio bytes")
						}
					}))
					defer srv.Close()
					cfg := &TTSConfig{Params: map[string]any{"temperature": 0.3, "voice": "voice"}}
					switch kind {
					case "minimax":
						cfg.Params["audio_setting"] = map[string]any{"format": "wav", "sample_rate": 32000}
						cfg.Params["voice_setting"] = map[string]any{"speed": 1}
					case "fishaudio":
						cfg.Params["format"] = "wav"
					default:
						cfg.Params["response_format"] = "wav"
					}
					if explicit {
						cfg.Format = "mp3"
					}
					before, _ := json.Marshal(cfg)
					key := " fixture "
					if kind == "openrouter" || kind == "fishaudio" {
						key = "fixture"
					}
					response, err := speechDriver(kind, srv.URL).AudioSpeech(speechString("audio/model"), stringPtr("hello"), &APIConfig{ApiKey: &key, Region: speechString("global")}, cfg)
					after, _ := json.Marshal(cfg)
					if err != nil || response == nil || string(response.Audio) != "audio bytes" {
						t.Fatal(response, err)
					}
					if !bytes.Equal(before, after) {
						t.Fatal("caller params mutated")
					}
				})
			}
		})
	}
}
func TestMiniMaxSpeechFailures(t *testing.T) {
	for _, raw := range []string{`{}`, `{"base_resp":{}}`, `{"base_resp":{"status_code":null}}`, `{"base_resp":{"status_code":42,"status_msg":"private-fixture"}}`, `{"base_resp":{"status_code":"0"}}`, `{"base_resp":{"status_code":0},"data":{"audio":"ab"}}`, `{"base_resp":{"status_code":0},"data":{"status":2,"audio":"odd"}}`, miniSpeechFrame("", 2), miniSpeechFrame("x", 1), `not JSON`} {
		t.Run(fmt.Sprintf("case-%x", []byte(raw)[:min(len(raw), 8)]), func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, raw) }))
			defer srv.Close()
			result, err := speechDriver("minimax", srv.URL).AudioSpeech(speechString("speech"), stringPtr("hello"), &APIConfig{ApiKey: speechString("fixture")}, nil)
			if err == nil || result != nil || strings.Contains(err.Error(), "private-fixture") {
				t.Fatal("false success or leaked body", err)
			}
		})
	}
}
func TestRawSpeechFailures(t *testing.T) {
	for _, kind := range []string{"fishaudio", "openrouter"} {
		for _, tc := range []struct {
			status     int
			mime, body string
		}{{401, "application/json", `{"detail":"private-fixture"}`}, {200, "application/json", `{"error":"private-fixture"}`}, {200, "text/plain", "audio"}, {200, "audio/mpeg", ""}, {200, "audio/wav", "audio"}} {
			t.Run(fmt.Sprintf("%s-%d-%s-%d", kind, tc.status, tc.mime, len(tc.body)), func(t *testing.T) {
				srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					w.Header().Set("Content-Type", tc.mime)
					w.WriteHeader(tc.status)
					io.WriteString(w, tc.body)
				}))
				defer srv.Close()
				result, err := speechDriver(kind, srv.URL).AudioSpeech(speechString("speech"), stringPtr("hello"), &APIConfig{ApiKey: speechString("fixture")}, nil)
				if kind == "openrouter" && tc.status == http.StatusOK && tc.mime == "audio/wav" {
					if err != nil || result == nil || string(result.Audio) != tc.body {
						t.Fatal("native audio MIME is valid without a requested format", err)
					}
					return
				}
				if kind == "fishaudio" && tc.status == 200 && strings.HasPrefix(tc.mime, "audio/") && tc.body != "" {
					if err != nil || result == nil || string(result.Audio) != tc.body {
						t.Fatal(result, err)
					}
					return
				}
				if err == nil || result != nil || strings.Contains(err.Error(), "private-fixture") {
					t.Fatal("false success or leaked body", err)
				}
			})
		}
	}
}
func TestSpeechInputDenialsDoNotCallProvider(t *testing.T) {
	var calls atomic.Int32
	srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1) }))
	defer srv.Close()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	for _, kind := range []string{"minimax", "fishaudio", "openrouter"} {
		driver := speechDriver(kind, srv.URL)
		for index, tc := range []struct {
			name *string
			text string
			api  *APIConfig
			cfg  *TTSConfig
		}{
			{nil, "hello", nil, nil}, {speechString("speech"), " ", nil, nil}, {speechString("speech"), "hello", nil, nil},
			{speechString("speech"), "hello", &APIConfig{ApiKey: speechString("fixture"), Region: speechString("missing")}, nil},
			{speechString("speech"), "hello", &APIConfig{ApiKey: speechString("fixture"), Context: ctx}, nil},
			{speechString("speech"), "hello", &APIConfig{ApiKey: speechString("fixture")}, &TTSConfig{Format: "../../bad"}},
			{speechString("speech"), "hello", &APIConfig{ApiKey: speechString("fixture")}, &TTSConfig{Params: map[string]any{"model": "other"}}},
			{speechString("speech"), "hello", &APIConfig{ApiKey: speechString("fixture")}, &TTSConfig{Params: map[string]any{"voice_setting": strings.Repeat("x", modelParamsLimitForTest)}}},
		} {
			if (kind == "minimax" || kind == "openrouter" || kind == "fishaudio") && index >= 5 {
				continue
			} // Frozen free Params/Format have no local allowlist.
			if _, err := driver.AudioSpeech(tc.name, stringPtr(tc.text), tc.api, tc.cfg); err == nil {
				t.Fatal("input accepted", kind)
			}
		}
	}
	if calls.Load() != 0 {
		t.Fatal("denial made an external call", calls.Load())
	}
}
func TestSpeechStreams(t *testing.T) {
	for _, kind := range []string{"minimax", "fishaudio"} {
		t.Run(kind, func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				var body map[string]any
				json.NewDecoder(r.Body).Decode(&body)
				if kind == "minimax" {
					if r.URL.Path != "/speech" || body["stream"] != true || body["stream_options"] != nil {
						t.Error("missing stream options")
					}
					io.WriteString(w, speechSSE(miniSpeechFrame("first", 1))+speechSSE(miniSpeechFrame("second", 1))+speechSSE(miniSpeechFrame("firstsecond", 2)))
				} else {
					if r.URL.Path != "/speech/stream/with-timestamp" || r.Header.Get("model") != "speech" {
						t.Error("wrong fish stream request")
					}
					for _, part := range []string{"first", "second"} {
						io.WriteString(w, speechSSE(fmt.Sprintf(`{"audio_base64":%q}`, base64.StdEncoding.EncodeToString([]byte(part)))))
					}
				}
			}))
			defer srv.Close()
			driver := speechDriver(kind, srv.URL).(ModelDriver)
			var got strings.Builder
			err := driver.AudioSpeechWithSender(speechString("speech"), stringPtr("hello"), &APIConfig{ApiKey: speechString("fixture")}, &TTSConfig{Format: "mp3"}, func(value, _ *string) error {
				if kind == "minimax" && value != nil && *value == "[DONE]" {
					return nil
				}
				r := &TTSResponse{Audio: modelStreamBytesForTest(value)}
				got.Write(r.Audio)
				return nil
			})
			if err != nil || got.String() != "firstsecond" {
				t.Fatal(got.String(), err)
			}
		})
	}
}
func TestSpeechStreamFailures(t *testing.T) {
	for _, kind := range []string{"minimax", "fishaudio"} {
		good := speechSSE(miniSpeechFrame("first", 1))
		if kind == "fishaudio" {
			good = speechSSE(`{"audio_base64":"Zmlyc3Q="}`)
		}
		cases := []string{"", "data: {bad}\n\n", good + "event: error\ndata: {\"private\":\"fixture\"}\n\n", good + "data: {}\n\n", strings.TrimSuffix(good, "\n\n")}
		if kind == "minimax" {
			cases = append(cases, good, good+speechSSE(miniSpeechFrame("different", 2)), good+speechSSE(`{"base_resp":{"status_code":7}}`), good+speechSSE(miniSpeechFrame("", 2))+good)
		} else {
			cases = append(cases, speechSSE(`{"audio_base64":"Zh=="}`), speechSSE(`{"audio_base64":"%%%"}`), speechSSE(`{"audio_base64":"","error":"private-fixture"}`))
		}
		for i, body := range cases {
			t.Run(fmt.Sprintf("%s-%d", kind, i), func(t *testing.T) {
				srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					w.Header().Set("Content-Type", "text/event-stream")
					io.WriteString(w, body)
				}))
				defer srv.Close()
				err := speechDriver(kind, srv.URL).(ModelDriver).AudioSpeechWithSender(speechString("speech"), stringPtr("hello"), &APIConfig{ApiKey: speechString("fixture")}, nil, func(value, _ *string) error { return nil })
				if kind == "fishaudio" && i == 4 {
					if err != nil {
						t.Fatal(err)
					}
					return
				}
				if err == nil || strings.Contains(err.Error(), "private-fixture") {
					t.Fatal("false success or leaked provider body", err)
				}
			})
		}
	}
}
func TestSpeechStreamSenderAndCancellation(t *testing.T) {
	for _, kind := range []string{"minimax", "fishaudio"} {
		for _, cancelRequest := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s-%v", kind, cancelRequest), func(t *testing.T) {
				closed := make(chan struct{})
				srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					defer close(closed)
					w.Header().Set("Content-Type", "text/event-stream")
					raw := miniSpeechFrame("first", 1)
					if kind == "fishaudio" {
						raw = `{"audio_base64":"Zmlyc3Q="}`
					}
					io.WriteString(w, speechSSE(raw))
					w.(http.Flusher).Flush()
					<-r.Context().Done()
				}))
				defer srv.Close()
				ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
				defer cancel()
				sentErr := errors.New("sender stopped")
				err := speechDriver(kind, srv.URL).(ModelDriver).AudioSpeechWithSender(speechString("speech"), stringPtr("hello"), &APIConfig{ApiKey: speechString("fixture"), Context: ctx}, nil, func(value, _ *string) error {
					if cancelRequest {
						cancel()
						return nil
					}
					return sentErr
				})
				if cancelRequest && !errors.Is(err, context.Canceled) || !cancelRequest && !errors.Is(err, sentErr) {
					t.Fatal("sender/cancellation lost", err)
				}
				select {
				case <-closed:
				case <-time.After(time.Second):
					t.Fatal("provider request leaked")
				}
			})
		}
	}
}
func TestSpeechResponseBoundsAndLargeSSE(t *testing.T) {
	for _, kind := range []string{"minimax", "fishaudio", "openrouter"} {
		t.Run(kind, func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "audio/mpeg")
				if kind == "minimax" {
					io.WriteString(w, strings.Repeat("x", 2*modelResponseLimitForTest+modelParamsLimitForTest+1))
				} else {
					io.CopyN(w, strings.NewReader(strings.Repeat("x", modelResponseLimitForTest+1)), modelResponseLimitForTest+1)
				}
			}))
			defer srv.Close()
			if _, err := speechDriver(kind, srv.URL).AudioSpeech(speechString("speech"), stringPtr("hello"), &APIConfig{ApiKey: speechString("fixture")}, nil); err == nil {
				t.Fatal("oversize success")
			}
		})
	}
	for _, kind := range []string{"minimax", "fishaudio"} {
		t.Run("large-"+kind, func(t *testing.T) {
			audio := strings.Repeat("a", 128*1024)
			if kind == "minimax" {
				audio = strings.Repeat("a", 256*1024)
			}
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				if kind == "minimax" {
					io.WriteString(w, speechSSE(miniSpeechFrame(audio, 1))+speechSSE(miniSpeechFrame(audio, 2)))
				} else {
					io.WriteString(w, speechSSE(fmt.Sprintf(`{"audio_base64":%q}`, base64.StdEncoding.EncodeToString([]byte(audio)))))
				}
			}))
			defer srv.Close()
			got := 0
			err := speechDriver(kind, srv.URL).(ModelDriver).AudioSpeechWithSender(speechString("speech"), stringPtr("hello"), &APIConfig{ApiKey: speechString("fixture")}, nil, func(value, _ *string) error {
				if kind == "minimax" && value != nil && *value == "[DONE]" {
					return nil
				}
				r := &TTSResponse{Audio: modelStreamBytesForTest(value)}
				got += len(r.Audio)
				return nil
			})
			if err != nil || got != len(audio) {
				t.Fatal("large SSE chunk lost", got, err)
			}
		})
	}
}
func TestFishAudioTranscription(t *testing.T) {
	for _, text := range []string{"spoken words", ""} {
		t.Run(text, func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/asr" || r.Header.Get("Authorization") != "Bearer fixture" {
					t.Error("wrong ASR metadata")
				}
				if err := r.ParseMultipartForm(1 << 20); err != nil {
					t.Error(err)
					return
				}
				defer r.MultipartForm.RemoveAll()
				file, header, err := r.FormFile("audio")
				if err != nil {
					t.Error(err)
					return
				}
				defer file.Close()
				audio, _ := io.ReadAll(file)
				if string(audio) != "client audio" || header.Filename != "model-input.wav" {
					t.Error("audio bytes/format lost")
				}
				if r.FormValue("language") != "en" || r.FormValue("prompt") != "hint" || r.FormValue("timestamps") != "true" || r.FormValue("temperature") != "0.25" || r.FormValue("nested") != "map[a:1]" {
					t.Error("ASR options lost")
				}
				json.NewEncoder(w).Encode(map[string]any{"text": text})
			}))
			defer srv.Close()
			cfg := &ASRConfig{Params: map[string]any{"timestamps": true, "temperature": 0.25, "nested": map[string]any{"a": 1}, "response_format": "mp3", "language": "en", "prompt": "hint"}}
			before, _ := json.Marshal(cfg)
			response, err := speechDriver("fishaudio", srv.URL).(ModelDriver).TranscribeAudio(speechString("transcribe-1"), modelFixtureFile(t, []byte("client audio")), &APIConfig{ApiKey: speechString("fixture")}, cfg)
			after, _ := json.Marshal(cfg)
			if err != nil || response == nil || response.Text != text || !bytes.Equal(before, after) {
				t.Fatal(response, err)
			}
		})
	}
	for _, raw := range []string{`{}`, `{"text":null}`, `{"text":1}`, `not JSON`, `{"text":"x","error":"private-fixture"}`, `{"text":"x","code":1}`} {
		t.Run("bad-"+raw, func(t *testing.T) {
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, raw) }))
			defer srv.Close()
			response, err := speechDriver("fishaudio", srv.URL).(ModelDriver).TranscribeAudio(speechString("transcribe-1"), modelFixtureFile(t, []byte("client audio")), &APIConfig{ApiKey: speechString("fixture")}, nil)
			if err == nil || response != nil || strings.Contains(err.Error(), "private-fixture") {
				t.Fatal("invalid ASR succeeded", err)
			}
		})
	}
}

func TestSpeechStreamCumulativeLimit(t *testing.T) {
	for _, kind := range []string{"minimax", "fishaudio"} {
		t.Run(kind, func(t *testing.T) {
			chunk := strings.Repeat("a", 64*1024)
			if kind == "minimax" {
				chunk = strings.Repeat("a", 256*1024)
			}
			frame := speechSSE(miniSpeechFrame(chunk, 1))
			if kind == "fishaudio" {
				frame = speechSSE(fmt.Sprintf(`{"audio_base64":%q}`, base64.StdEncoding.EncodeToString([]byte(chunk))))
			}
			srv := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "text/event-stream")
				if kind == "minimax" {
					for range 81 {
						io.WriteString(w, frame)
					}
				} else {
					for range 321 {
						io.WriteString(w, frame)
					}
				}
			}))
			defer srv.Close()
			sent := 0
			err := speechDriver(kind, srv.URL).(ModelDriver).AudioSpeechWithSender(speechString("speech"), stringPtr("hello"), &APIConfig{ApiKey: speechString("fixture")}, nil, func(value, _ *string) error { sent++; return nil })
			wantSent := 320
			if kind == "minimax" {
				wantSent = 80
			}
			if err == nil || sent != wantSent || !strings.Contains(err.Error(), "limit") && !strings.Contains(err.Error(), "20 MiB") {
				t.Fatal("stream crossed cumulative byte limit", sent, err)
			}
		})
	}
}
