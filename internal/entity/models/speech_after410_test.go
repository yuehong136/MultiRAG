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
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
)

func after410SpeechDriver(provider, base string) ModelDriver {
	if provider == "SiliconFlow" {
		return NewSiliconFlowModel(map[string]string{"default": base}, URLSuffix{TTS: "speech", ASR: "asr"})
	}
	return NewStepFunModel(map[string]string{"default": base}, URLSuffix{TTS: "speech"})
}

func TestAfter410SpeechProvidersHTTP(t *testing.T) {
	key, name := "controlled-speech-key", "model/name"
	for _, provider := range []string{"SiliconFlow", "StepFun"} {
		t.Run(provider, func(t *testing.T) {
			audio := bytes.Repeat([]byte{0, 255, 3, 10}, 20000)
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/speech" || r.Header.Get("Authorization") != "Bearer "+key {
					t.Error("speech path/credentials lost")
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				if body["model"] != name || body["input"] != "hello" || body["response_format"] != "wav" || body["voice"] != "selected" {
					t.Error("speech payload lost", body)
				}
				stream := body["stream"] == true || body["stream_format"] == "sse"
				if provider == "SiliconFlow" && (body["sample_rate"] != float64(32000) || body["speed"] != float64(1) || body["gain"] != float64(0)) {
					t.Error("SiliconFlow defaults lost", body)
				}
				if stream && provider == "StepFun" {
					w.Header().Set("Content-Type", "text/event-stream")
					frame := fmt.Sprintf("data: {\"type\":\"speech.audio.delta\",\"audio\":%q}\n\n", base64.StdEncoding.EncodeToString(audio))
					io.WriteString(w, frame[:23])
					w.(http.Flusher).Flush()
					io.WriteString(w, frame[23:])
					io.WriteString(w, "data: {\"type\":\"speech.audio.done\"}\n\ndata: [DONE]\n\n")
				} else {
					w.Header().Set("Content-Type", "audio/wav")
					w.Write(audio[:10])
					w.(http.Flusher).Flush()
					w.Write(audio[10:])
				}
			}))
			defer srv.Close()
			d := after410SpeechDriver(provider, srv.URL)
			config := &TTSConfig{Format: "wav", Voice: "selected", Params: map[string]any{"voice": "other", "response_format": "mp3"}}
			r, err := d.(TTSDriver).AudioSpeech(&name, "hello", &APIConfig{APIKey: &key}, config)
			if err != nil || !bytes.Equal(r.Audio, audio) || r.Format != "wav" {
				t.Fatal(r, err)
			}
			var combined []byte
			err = d.(TTSStreamDriver).AudioSpeechWithSender(&name, "hello", &APIConfig{APIKey: &key}, config, func(r *TTSResponse) error {
				if r.Format != "wav" {
					t.Error("stream format lost")
				}
				combined = append(combined, r.Audio...)
				return nil
			})
			if err != nil || !bytes.Equal(combined, audio) {
				t.Fatal("stream bytes lost", len(combined), err)
			}
			if config.Params["voice"] != "other" || config.Params["response_format"] != "mp3" {
				t.Fatal("caller config mutated")
			}
			stop := errors.New("sender stopped")
			if err := d.(TTSStreamDriver).AudioSpeechWithSender(&name, "hello", &APIConfig{APIKey: &key}, config, func(*TTSResponse) error { return stop }); !errors.Is(err, stop) {
				t.Fatal("sender error lost", err)
			}
		})
	}
	body, format, err := siliconFlowSpeechBody(&name, "hello", nil, false)
	if err != nil || format != "mp3" || body["voice"] != name+":anna" || body["sample_rate"] != 32000 {
		t.Fatal(body, format, err)
	}
}

func TestAfter410SpeechFailures(t *testing.T) {
	name, key := "model", "controlled-key"
	for _, provider := range []string{"SiliconFlow", "StepFun"} {
		for _, failure := range []string{"http", "json-audio", "empty", "truncated", "cancelled"} {
			t.Run(provider+"/"+failure, func(t *testing.T) {
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if failure == "http" {
						w.WriteHeader(502)
						fmt.Fprint(w, "private-body-"+key)
						return
					}
					if failure == "json-audio" {
						w.Header().Set("Content-Type", "application/json")
						fmt.Fprint(w, `{"error":"private-body"}`)
						return
					}
					w.Header().Set("Content-Type", "audio/mpeg")
					if failure == "truncated" {
						w.Header().Set("Content-Length", "100")
						fmt.Fprint(w, "short")
					}
				}))
				defer srv.Close()
				d := after410SpeechDriver(provider, srv.URL)
				api := &APIConfig{APIKey: &key}
				if failure == "cancelled" {
					ctx, cancel := context.WithCancel(context.Background())
					cancel()
					api.Context = ctx
				}
				if _, err := d.(TTSDriver).AudioSpeech(&name, "hello", api, nil); err == nil || strings.Contains(err.Error(), "private-body") || strings.Contains(err.Error(), key) {
					t.Fatal("speech failure lost/leaked", err)
				}
				if err := d.(TTSStreamDriver).AudioSpeechWithSender(&name, "hello", api, nil, func(*TTSResponse) error { return nil }); err == nil {
					t.Fatal("speech stream failure lost")
				}
			})
		}
	}
	for _, raw := range []string{
		"data: {bad}\n\n", "data: {\"type\":\"speech.audio.delta\",\"audio\":\"%%%\"}\n\n",
		"data: {\"type\":\"speech.audio.error\"}\n\n", "event: error\ndata: {}\n\n", "data: [DONE]\n\n",
		"data: {\"type\":\"speech.audio.delta\",\"audio\":\"YQ==\"}\n\n",
		"data: {\"type\":\"speech.audio.delta\",\"audio\":\"YQ==\"}\n",
		"data: [DONE]\n\ndata: {\"type\":\"speech.audio.delta\",\"audio\":\"YQ==\"}\n\n",
	} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/event-stream")
			fmt.Fprint(w, raw)
		}))
		d := after410SpeechDriver("StepFun", srv.URL)
		if err := d.(TTSStreamDriver).AudioSpeechWithSender(&name, "hello", &APIConfig{APIKey: &key}, nil, func(*TTSResponse) error { return nil }); err == nil {
			t.Fatal("bad StepFun stream accepted", raw)
		}
		srv.Close()
	}
}

func TestSiliconFlowASRMultipartContract(t *testing.T) {
	key, name := "controlled-key", "FunAudioLLM/SenseVoiceSmall"
	response := `{"text":"transcription"}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
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
			names = append(names, part.FormName())
			data, _ := io.ReadAll(part)
			switch part.FormName() {
			case "file":
				if string(data) != "audio bytes" || part.FileName() != "audio.mp3" {
					t.Error("audio bytes lost")
				}
			case "model":
				if string(data) != name {
					t.Error("ASR model lost")
				}
			case "language":
				if string(data) != "en" {
					t.Error("ASR language lost")
				}
			}
		}
		if !reflect.DeepEqual(names, []string{"model", "language", "prompt", "temperature", "file"}) {
			t.Error("ASR fields must precede file", names)
		}
		fmt.Fprint(w, response)
	}))
	defer srv.Close()
	d := NewSiliconFlowModel(map[string]string{"default": srv.URL}, URLSuffix{ASR: "asr"})
	config := &ASRConfig{Format: "mp3", Language: []string{"en"}, Prompt: "context", Params: map[string]any{"temperature": 0}}
	if r, err := d.TranscribeAudio(&name, []byte("audio bytes"), &APIConfig{APIKey: &key}, config); err != nil || *r.Text != "transcription" {
		t.Fatal(r, err)
	}
	for _, bad := range []string{`{}`, `{"text":null}`, `{"error":"private-body"}`, `{"code":9,"text":"no"}`, `bad`} {
		response = bad
		if _, err := d.TranscribeAudio(&name, []byte("audio bytes"), &APIConfig{APIKey: &key}, config); err == nil || strings.Contains(err.Error(), "private-body") {
			t.Fatal("bad ASR accepted/leaked", err)
		}
	}
}
