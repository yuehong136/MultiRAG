package models

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"testing"
	"time"
)

func timeoutTestClient(t *testing.T, driver ModelDriver) *http.Client {
	t.Helper()
	switch d := driver.(type) {
	case *MinimaxModel:
		return d.httpClient
	case *AliyunModel:
		return d.httpClient
	case *DeepSeekModel:
		return d.httpClient
	case *GiteeModel:
		return d.httpClient
	case *SiliconFlowModel:
		return d.httpClient
	case *ZhipuAIModel:
		return d.httpClient
	case *VLLMModel:
		return d.httpClient
	case *VolcEngine:
		return d.httpClient
	case *LMStudioModel:
		return d.httpClient
	case *MoonshotModel:
		return d.httpClient
	case *OllamaModel:
		return d.httpClient
	case *XAIModel:
		return d.httpClient
	case *GroqModel:
		return d.httpClient
	case *OpenAIModel:
		return d.httpClient
	case *FishAudioModel:
		return d.driver.httpClient
	case *DeepInfraModel:
		return d.httpClient
	case *TogetherAIModel:
		return d.httpClient
	case *StepFunModel:
		return d.httpClient
	case *XinferenceModel:
		return d.httpClient
	case *CometAPIModel:
		return d.httpClient
	case *PaddleOCRModel:
		return d.httpClient
	case *PaddleOCRLocalModel:
		return d.httpClient
	case *MinerULocalModel:
		return d.httpClient
	case *MinerUModel:
		return d.httpClient
	case *OpenRouterModel:
		return d.httpClient
	case *CoHereModel:
		return d.httpClient
	case *AI302Model:
		return d.httpClient
	default:
		t.Fatalf("unsupported timeout test driver %T", driver)
		return nil
	}
}

// Scale only the observed HTTP deadline, after checking the real operation
// budget. No global timeout mutation or production test override is required.
func scaledTimeoutClient(t *testing.T, client *http.Client, want time.Duration) {
	t.Helper()
	if client.Timeout != 0 {
		t.Fatalf("whole-response client timeout = %s", client.Timeout)
	}
	client.Transport = lmStudioTransportFunc(func(req *http.Request) (*http.Response, error) {
		deadline, ok := req.Context().Deadline()
		remaining := time.Until(deadline)
		if !ok || remaining < want-time.Second || remaining > want {
			t.Errorf("request budget %s, want %s", remaining, want)
		}
		budget := 40 * time.Millisecond
		if want > nonStreamCallTimeout {
			budget = time.Second
		}
		ctx, cancel := context.WithTimeout(req.Context(), budget)
		response, err := http.DefaultTransport.RoundTrip(req.Clone(ctx))
		if err != nil {
			cancel()
			return nil, err
		}
		response.Body = &timeoutTestBody{ReadCloser: response.Body, cancel: cancel}
		return response, nil
	})
}

type timeoutTestBody struct {
	io.ReadCloser
	cancel context.CancelFunc
}

func (b *timeoutTestBody) Close() error { defer b.cancel(); return b.ReadCloser.Close() }

func TestCallTimeoutChatHTTP(t *testing.T) {
	for _, provider := range []string{"MiniMax", "Aliyun", "DeepSeek", "Gitee", "SiliconFlow", "ZHIPU-AI", "vllm", "VolcEngine", "LM-Studio", "Moonshot", "xAI", "Groq"} {
		t.Run(provider, func(t *testing.T) {
			server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					return
				}
				if body["stream"] != true {
					// Real headers/body wait must be interrupted by the ordinary deadline.
					<-r.Context().Done()
					return
				}
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"one\"}}]}\n\n")
				w.(http.Flusher).Flush()
				select {
				case <-r.Context().Done():
					return
				case <-time.After(100 * time.Millisecond):
				}
				fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"two\"}}]}\n\ndata: [DONE]\n\n")
			}))
			driver, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
			if err != nil {
				t.Fatal(err)
			}
			api := &APIConfig{APIKey: stringPtr("fixture")}
			client := timeoutTestClient(t, driver)
			scaledTimeoutClient(t, client, streamCallTimeout)
			var got []string
			err = driver.ChatStreamlyWithSender(stringPtr("served"), stringPtr("hello"), api, nil, func(c, r *string) error {
				if c != nil {
					got = append(got, *c)
				}
				return nil
			})
			if err != nil || strings.Join(got, "") != "onetwo[DONE]" || len(got) == 0 || got[len(got)-1] != "[DONE]" {
				t.Fatal(got, err)
			}
			scaledTimeoutClient(t, client, nonStreamCallTimeout)
			_, err = driver.ChatWithMessages("served", api, []Message{{Role: "user", Content: "hello"}}, nil)
			if !errors.Is(err, context.DeadlineExceeded) {
				t.Fatal("ordinary deadline not enforced", err)
			}
			if api.Context != nil {
				t.Fatal("caller config mutated")
			}
		})
	}
}

func TestCallTimeoutParentAndCleanup(t *testing.T) {
	for _, failure := range []string{"headers", "body", "sender"} {
		t.Run(failure, func(t *testing.T) {
			stopped := make(chan struct{})
			server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				io.Copy(io.Discard, r.Body)
				if failure != "headers" {
					w.Header().Set("Content-Type", "text/event-stream")
					fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"part\"}}]}\n\n")
					w.(http.Flusher).Flush()
				}
				<-r.Context().Done()
				close(stopped)
			}))
			driver := NewMinimaxModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
			ctx, cancel := context.WithTimeout(context.Background(), 80*time.Millisecond)
			defer cancel()
			terminal := false
			sentinel := errors.New("sender stopped")
			err := driver.ChatStreamlyWithSender(stringPtr("model"), stringPtr("q"), &APIConfig{APIKey: stringPtr("key"), Context: ctx}, nil, func(c, r *string) error {
				if c != nil && *c == "[DONE]" {
					terminal = true
				}
				if failure == "sender" {
					return sentinel
				}
				return nil
			})
			want := error(context.DeadlineExceeded)
			if failure == "sender" {
				want = sentinel
			}
			if !errors.Is(err, want) || terminal {
				t.Fatal(err, terminal)
			}
			select {
			case <-stopped:
			case <-time.After(time.Second):
				t.Fatal("response body or request leaked")
			}
		})
	}
}

func TestCallTimeoutMinimaxConcurrentInstances(t *testing.T) {
	servers := map[string]string{}
	for _, key := range []string{"one", "two"} {
		server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Header.Get("Authorization") != "Bearer "+key {
				t.Error("cross-instance credentials")
			}
			fmt.Fprintf(w, `{"choices":[{"message":{"content":%q}}]}`, key)
		}))
		servers[key] = server.URL
	}
	driver := NewMinimaxModel(servers, URLSuffix{Chat: "chat"})
	var group sync.WaitGroup
	for i := 0; i < 16; i++ {
		group.Add(1)
		go func(i int) {
			defer group.Done()
			key := "one"
			if i%2 == 1 {
				key = "two"
			}
			api := &APIConfig{APIKey: &key, Region: &key}
			response, err := driver.ChatWithMessages("model", api, []Message{{Role: "user", Content: "q"}}, nil)
			if err != nil || response == nil || *response.Answer != key {
				t.Error(response, err)
			}
		}(i)
	}
	group.Wait()
}

func TestCallTimeoutMinimaxRealLongStream(t *testing.T) {
	duration := 100 * time.Millisecond
	// Explicit acceptance can exercise the original 120s boundary with no
	// transport scaling. Routine regression remains fast and uses the same path.
	if raw := os.Getenv("MULTIRAG_TEST_STREAM_DURATION"); raw != "" {
		var err error
		duration, err = time.ParseDuration(raw)
		if err != nil {
			t.Fatal(err)
		}
	}
	server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"begin\"}}]}\n\n")
		w.(http.Flusher).Flush()
		select {
		case <-r.Context().Done():
			return
		case <-time.After(duration):
		}
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"end\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n")
	}))
	driver := NewMinimaxModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"})
	var got []string
	start := time.Now()
	err := driver.ChatStreamlyWithSender(stringPtr("model"), stringPtr("q"), &APIConfig{APIKey: stringPtr("fixture")}, nil, func(c, r *string) error {
		if c != nil {
			got = append(got, *c)
		}
		return nil
	})
	if err != nil || strings.Join(got, "|") != "begin|end|[DONE]" || time.Since(start) < duration {
		t.Fatal(got, err)
	}
	t.Logf("real HTTP stream duration=%s client.Timeout=%s", time.Since(start), driver.httpClient.Timeout)
}

func TestCallTimeoutMediaBudgets(t *testing.T) {
	for _, row := range []struct {
		provider   string
		operations []string
	}{
		{"MiniMax", []string{"tts", "tts-stream"}}, {"FishAudio", []string{"tts", "tts-stream", "asr"}},
		{"SiliconFlow", []string{"tts", "tts-stream", "asr"}}, {"OpenAI", []string{"tts", "tts-stream", "asr"}},
		{"Groq", []string{"tts", "asr"}}, {"TogetherAI", []string{"tts", "tts-stream", "asr"}},
		{"StepFun", []string{"tts", "tts-stream"}}, {"Xinference", []string{"tts", "asr"}},
		{"CometAPI", []string{"tts", "asr"}}, {"DeepInfra", []string{"tts", "tts-stream", "asr"}},
		{"OpenRouter", []string{"tts", "asr"}}, {"xAI", []string{"tts", "tts-stream", "asr"}},
		{"ZHIPU-AI", []string{"tts", "tts-stream", "asr", "ocr"}}, {"302.AI", []string{"ocr", "parse", "asr"}},
		{"PaddleOCR", []string{"ocr"}}, {"PaddleOCR_Local", []string{"ocr"}},
		{"MinerU", []string{"parse"}}, {"MinerU_Local", []string{"parse"}}, {"Cohere", []string{"asr"}},
	} {
		provider := row.provider
		for _, operation := range row.operations {
			t.Run(provider+"/"+operation, func(t *testing.T) {
				driver, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": "http://fixture.invalid"}, URLSuffix{TTS: "speech", ASR: "asr", OCR: "ocr", DocumentParse: "parse"})
				if err != nil {
					t.Fatal(err)
				}
				client := timeoutTestClient(t, driver)
				if client.Timeout != 0 {
					t.Fatal("media inherits whole-response timeout", client.Timeout)
				}
				calls := 0
				client.Transport = lmStudioTransportFunc(func(req *http.Request) (*http.Response, error) {
					calls++
					deadline, ok := req.Context().Deadline()
					remaining := time.Until(deadline)
					if !ok || remaining < longOpCallTimeout-time.Second || remaining > longOpCallTimeout {
						t.Errorf("media budget=%s", remaining)
					}
					return nil, errors.New("controlled transport stop")
				})
				api := &APIConfig{APIKey: stringPtr("fixture")}
				name := stringPtr("served")
				switch operation {
				case "tts":
					if d, ok := driver.(TTSDriver); ok {
						_, err = d.AudioSpeech(name, "hello", api, &TTSConfig{Voice: "voice", Format: "mp3"})
					}
				case "tts-stream":
					if d, ok := driver.(TTSStreamDriver); ok {
						err = d.AudioSpeechWithSender(name, "hello", api, &TTSConfig{Voice: "voice", Format: "mp3"}, func(*TTSResponse) error { return nil })
					}
				case "asr":
					if d, ok := driver.(ASRDriver); ok {
						_, err = d.TranscribeAudio(name, []byte("audio"), api, nil)
					}
				case "ocr":
					if d, ok := driver.(OCRDriver); ok {
						_, err = d.OCRFile(name, []byte("image"), api, nil)
					}
				case "parse":
					if d, ok := driver.(FileParser); ok {
						if provider == "MinerU_Local" {
							_, err = d.ParseFile(name, []byte("document"), "", api)
						} else {
							_, err = d.ParseFile(name, nil, "https://example.com/document.pdf", api)
						}
					}
				}
				if calls != 1 || err == nil {
					t.Fatal("expected exactly one failing transport request", calls, err)
				}

				t.Logf("actual requests=%d", calls)
			})
		}
	}
}

func TestCallTimeoutCanceledBufferedTerminal(t *testing.T) {
	server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"part\"}}]}\n\ndata: [DONE]\n\n")
	}))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	terminal := false
	err := NewMinimaxModel(map[string]string{"default": server.URL}, URLSuffix{Chat: "chat"}).ChatStreamlyWithSender(stringPtr("model"), stringPtr("q"), &APIConfig{APIKey: stringPtr("key"), Context: ctx}, nil, func(c, r *string) error {
		if c != nil && *c == "[DONE]" {
			terminal = true
		}
		cancel()
		return nil
	})
	if !errors.Is(err, context.Canceled) || terminal {
		t.Fatal(err, terminal)
	}
}

func TestCallTimeoutGiteeParseScope(t *testing.T) {
	driver := NewGiteeModel(map[string]string{"default": "http://fixture.invalid"}, URLSuffix{DocumentParse: "parse", Task: "task"})
	calls := 0
	driver.httpClient.Transport = lmStudioTransportFunc(func(req *http.Request) (*http.Response, error) {
		calls++
		deadline, ok := req.Context().Deadline()
		remaining := time.Until(deadline)
		if !ok || remaining < longOpCallTimeout-time.Second || remaining > longOpCallTimeout {
			t.Errorf("parse result inherited short child scope %s", remaining)
		}
		raw := `{"task_id":"job","status":"success"}`
		if strings.HasSuffix(req.URL.Path, "/get") {
			raw = `{"segments":[{"index":0,"content":"text"}]}`
		}
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(raw))}, nil
	})
	task, err := driver.ParseFile(stringPtr("model"), []byte("document"), "", &APIConfig{APIKey: stringPtr("key")})
	if err != nil || task == nil || len(task.Segments) != 1 || calls != 3 {
		t.Fatal(task, err, calls)
	}
}
