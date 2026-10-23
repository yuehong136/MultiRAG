package handler_test

import (
	"bytes"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/utility"
)

// Real CLI processes, registered JWT-protected routes, SQL instances and actual
// drivers. Only the external providers are controlled HTTP servers.
func TestSpeechProvidersHTTPCLIAndSQL(t *testing.T) {
	config, binary := os.Getenv("MULTIRAG_SPEECH_CONFIG"), os.Getenv("MULTIRAG_SPEECH_CLI")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned speech scratch config required")
		}
		t.Skip("requires owned speech scratch database")
	}
	if binary == "" {
		t.Fatal("candidate speech CLI binary required")
	}
	t.Log("owned-test-process", os.Getpid())
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal("load owned speech config")
	}
	if !regexp.MustCompile(`^multirag_after1c_b_speech_[a-z0-9_]{6,32}$`).MatchString(server.GetConfig().Database.Database) {
		t.Fatal("unowned speech database")
	}
	if err := dao.InitDB(); err != nil {
		t.Fatal("initialize owned speech database", err)
	}
	pool, err := dao.DB.DB()
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	dsn, err := dao.PostgresDSN(server.GetConfig().Database)
	if err != nil {
		t.Fatal(err)
	}
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer readback.Close()
	if err := server.InitVariables(nil); err != nil {
		t.Fatal(err)
	}
	local.InitAdminStatus(0, "owned speech acceptance")
	active := "1"
	tokens := map[string]string{}
	for _, id := range []string{"speech-owner", "speech-foreign", "speech-revoked", "speech-disabled"} {
		status := active
		if id == "speech-disabled" {
			status = "0"
		}
		email := id + "@speech.invalid"
		if err := dao.DB.Create(&entity.User{ID: id, Email: email, Nickname: id, Status: &status}).Error; err != nil {
			t.Fatal(err)
		}
		relation := active
		if id == "speech-revoked" {
			relation = "0"
		}
		if err := dao.DB.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: id + "-tenant", Role: "owner", InvitedBy: id, Status: &relation}).Error; err != nil {
			t.Fatal(err)
		}
		token, err := utility.SignUserJWT(email, server.GetSecretKey())
		if err != nil {
			t.Fatal(err)
		}
		tokens[id] = token
	}
	var calls atomic.Int32
	var providers []*httptest.Server
	t.Cleanup(func() {
		for _, p := range providers {
			p.Close()
		}
	})
	for pi, provider := range []string{"MiniMax", "FishAudio", "OpenRouter"} {
		providerID := fmt.Sprintf("speech-provider-%d", pi)
		if err := dao.DB.Create(&entity.TenantModelProvider{ID: providerID, TenantID: "speech-owner-tenant", ProviderName: provider}).Error; err != nil {
			t.Fatal(err)
		}
		for _, instance := range []string{"one", "two"} {
			key := fmt.Sprintf("controlled-speech-%d-%s", pi, instance)
			audio := "speech-audio-" + provider + "-" + instance
			fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.Header.Get("Authorization") != "Bearer "+key {
					t.Error("SQL-selected provider key crossed instances")
				}
				if provider == "FishAudio" && strings.HasSuffix(r.URL.Path, "/asr") {
					if r.Header.Get("model") != "transcribe-1" {
						t.Error("ASR model header lost")
					}
					if err := r.ParseMultipartForm(1 << 20); err != nil {
						t.Error(err)
						w.WriteHeader(400)
						return
					}
					defer r.MultipartForm.RemoveAll()
					file, header, err := r.FormFile("audio")
					if err != nil {
						t.Error(err)
						w.WriteHeader(400)
						return
					}
					defer file.Close()
					data, _ := io.ReadAll(file)
					if string(data) != "client audio bytes" || header.Filename != "audio.wav" || r.FormValue("language") != "en" || r.FormValue("ignore_timestamps") != "true" {
						t.Error("ASR bytes/format/params lost")
					}
					json.NewEncoder(w).Encode(map[string]any{"text": "transcribed-" + instance})
					return
				}
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
					w.WriteHeader(400)
					return
				}
				text, _ := body["text"].(string)
				if provider == "OpenRouter" {
					text, _ = body["input"].(string)
				}
				if text == "provider-error" {
					w.WriteHeader(502)
					fmt.Fprint(w, `{"error":"private-provider-response"}`)
					return
				}
				format := "wav"
				if provider == "MiniMax" {
					if r.URL.Path != "/v1/t2a_v2" || body["model"] != "speech-2.8-hd" {
						t.Error("MiniMax model/path lost")
					}
					setting, _ := body["audio_setting"].(map[string]any)
					voice, _ := body["voice_setting"].(map[string]any)
					if setting["sample_rate"] != float64(32000) || voice["voice_id"] != "fixture-voice" || voice["speed"] != float64(1) {
						t.Error("MiniMax nested params lost")
					}
					format, _ = setting["format"].(string)
				} else if provider == "FishAudio" {
					if r.Header.Get("model") != "s2-pro" || body["reference_id"] != "fixture-voice" {
						t.Error("FishAudio model/params lost")
					}
					format, _ = body["format"].(string)
				} else {
					if r.URL.Path != "/api/v1/audio/speech" || body["model"] != "openai/gpt-audio-mini" || body["voice"] != "fixture-voice" {
						t.Error("OpenRouter path/model/params lost")
					}
					format, _ = body["response_format"].(string)
				}
				if format != "wav" {
					t.Error("explicit FORMAT not applied")
				}
				switch {
				case provider == "MiniMax":
					frame := func(part string, status int) string {
						return fmt.Sprintf(`{"base_resp":{"status_code":0},"data":{"audio":%q,"status":%d},"extra_info":{"audio_format":"wav"}}`, hex.EncodeToString([]byte(part)), status)
					}
					if body["stream"] == true {
						w.Header().Set("Content-Type", "text/event-stream")
						fmt.Fprintf(w, "data: %s\n\n", frame(audio, 1))
						if text != "missing-final" {
							fmt.Fprintf(w, "data: %s\n\n", frame("", 2))
						}
					} else {
						fmt.Fprint(w, frame(audio, 2))
					}
				case provider == "FishAudio" && strings.HasSuffix(r.URL.Path, "/stream/with-timestamp"):
					w.Header().Set("Content-Type", "text/event-stream")
					fmt.Fprintf(w, "data: {\"audio_base64\":%q}\n", base64.StdEncoding.EncodeToString([]byte(audio)))
					if text != "missing-final" {
						fmt.Fprint(w, "\n")
					}
				default:
					w.Header().Set("Content-Type", "audio/wav")
					fmt.Fprint(w, audio)
				}
			}))
			providers = append(providers, fixture)
			t.Log("owned-provider-listener", fixture.URL)
			instanceID := fmt.Sprintf("speech-instance-%d-%s", pi, instance)
			base := fixture.URL
			if provider == "OpenRouter" {
				base += "/api/v1"
			}
			extra, _ := json.Marshal(map[string]string{"base_url": base, "region": "global"})
			if err := dao.DB.Create(&entity.TenantModelInstance{ID: instanceID, ProviderID: providerID, InstanceName: instance, Status: "active", APIKey: key, Extra: string(extra)}).Error; err != nil {
				t.Fatal(err)
			}
			modelName := "speech-2.8-hd"
			if provider == "FishAudio" {
				modelName = "s2-pro"
			}
			if provider == "OpenRouter" {
				modelName = "openai/gpt-audio-mini"
			}
			for _, model := range []struct{ name, typ, status string }{{modelName, "tts", "active"}, {"transcribe-1", "speech2text", "active"}, {"wrong-type", "chat", "active"}, {"disabled-voice", "tts", "disabled"}} {
				details, _ := json.Marshal(map[string]any{"max_tokens": 8192, "model_types": []string{model.typ}})
				if err := dao.DB.Create(&entity.TenantModel{ID: fmt.Sprintf("s%d-%s-%s", pi, instance, model.name[:min(len(model.name), 12)]), ProviderID: providerID, InstanceID: instanceID, ModelName: model.name, ModelType: model.typ, Status: model.status, Extra: string(details)}).Error; err != nil {
					t.Fatal(err)
				}
			}
		}
	}
	snapshot := func() string {
		t.Helper()
		var s string
		if err := readback.QueryRow(`SELECT (SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM tenant_model_provider x)||(SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM tenant_model_instance x)||(SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM tenant_model x)||(SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM t_ai_users x)||(SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM t_ai_user_tenants x)`).Scan(&s); err != nil {
			t.Fatal(err)
		}
		return s
	}
	before := snapshot()
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	app.Use(gin.Recovery())
	svc := service.NewModelProviderService()
	routes := router.NewRouter(handler.NewAuthHandler(), nil, nil, nil, nil, handler.NewSystemHandler(service.NewSystemService()), nil, nil, nil, nil, nil, nil, nil, nil, nil, handler.NewProviderHandler(nil, svc), nil)
	routes.Setup(app)
	api := httptest.NewServer(app)
	t.Cleanup(api.Close)
	t.Log("owned-api-listener", api.URL)
	request := func(identity, path string, body map[string]any) (map[string]any, string) {
		t.Helper()
		raw, _ := json.Marshal(body)
		req, err := http.NewRequestWithContext(t.Context(), http.MethodPost, api.URL+"/api/v1"+path, bytes.NewReader(raw))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Content-Type", "application/json")
		if identity != "" {
			req.Header.Set("Authorization", "Bearer "+tokens[identity])
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		data, err := io.ReadAll(resp.Body)
		if err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream") && resp.Header.Get("Content-Type") != "application/json; charset=utf-8" {
			t.Error("API MIME contract changed")
		}
		var out map[string]any
		if !strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream") {
			if err := json.Unmarshal(data, &out); err != nil {
				t.Fatal(err)
			}
		}
		return out, string(data)
	}
	speechPayload := func(provider, model, instance, text string) map[string]any {
		params := map[string]any{"voice": "fixture-voice"}
		if provider == "MiniMax" {
			params = map[string]any{"voice_setting": map[string]any{"voice_id": "fixture-voice", "speed": 1}, "audio_setting": map[string]any{"format": "mp3", "sample_rate": 32000}}
		}
		if provider == "FishAudio" {
			params = map[string]any{"reference_id": "fixture-voice"}
		}
		return map[string]any{"provider_name": provider, "instance_name": instance, "model_name": model, "text": text, "tts_config": map[string]any{"format": "wav", "params": params}}
	}
	for _, provider := range []string{"MiniMax", "FishAudio", "OpenRouter"} {
		model := "speech-2.8-hd"
		if provider == "FishAudio" {
			model = "s2-pro"
		}
		if provider == "OpenRouter" {
			model = "openai/gpt-audio-mini"
		}
		for _, instance := range []string{"one", "two"} {
			payload := speechPayload(provider, model, instance, "hello")
			out, raw := request("speech-owner", "/audio/speech", payload)
			if out["code"] != float64(0) {
				t.Fatal("speech HTTP failed", raw)
			}
			data := out["data"].(map[string]any)
			audio, err := base64.StdEncoding.Strict().DecodeString(data["audio"].(string))
			if err != nil || string(audio) != "speech-audio-"+provider+"-"+instance || data["format"] != "wav" {
				t.Fatal("speech bytes or format lost")
			}
			payload["stream"] = true
			_, raw = request("speech-owner", "/audio/speech", payload)
			if provider == "OpenRouter" {
				if !strings.Contains(raw, `"code":501`) || strings.Contains(raw, "event: done") {
					t.Fatal("unsupported stream falsely succeeded")
				}
			} else if !strings.Contains(raw, "event: done") || !strings.Contains(raw, "[DONE]") {
				t.Fatal("speech stream did not complete", raw)
			}
		}
	}
	// Concurrent calls to two independent SQL-selected credentials use the actual drivers.
	var parallel sync.WaitGroup
	for i := range 8 {
		parallel.Go(func() {
			instance := "one"
			if i%2 == 1 {
				instance = "two"
			}
			out, _ := request("speech-owner", "/audio/speech", speechPayload("FishAudio", "s2-pro", instance, "hello"))
			if out["code"] != float64(0) {
				t.Error("concurrent speech failed")
			}
		})
	}
	parallel.Wait()
	count := calls.Load()
	for _, identity := range []string{"", "speech-foreign", "speech-revoked", "speech-disabled"} {
		out, raw := request(identity, "/audio/speech", speechPayload("MiniMax", "speech-2.8-hd", "one", "hello"))
		if out["code"] == float64(0) || out["data"] != nil {
			t.Fatal("identity denial succeeded", raw)
		}
	}
	for _, model := range []string{"wrong-type", "disabled-voice", "missing"} {
		out, raw := request("speech-owner", "/audio/speech", speechPayload("MiniMax", model, "one", "hello"))
		if out["code"] == float64(0) || out["data"] != nil {
			t.Fatal("model denial succeeded", raw)
		}
	}
	for _, body := range []map[string]any{
		{"tts_config": map[string]any{"params": map[string]any{"model": "override"}}},
		{"tts_config": map[string]any{"params": map[string]any{"stream": true}}},
		{"tts_config": map[string]any{"format": "ogg"}},
		{"format": "mp3"},
		{"text": " "},
		{"instance_name": "missing"},
	} {
		p := speechPayload("MiniMax", "speech-2.8-hd", "one", "hello")
		for k, v := range body {
			p[k] = v
		}
		out, raw := request("speech-owner", "/audio/speech", p)
		if out["code"] == float64(0) || out["data"] != nil {
			t.Fatal("bad input succeeded", raw)
		}
	}
	for _, file := range []any{"/etc/passwd", "%%%", []int{1, 2}} {
		out, raw := request("speech-owner", "/audio/transcriptions", map[string]any{"provider_name": "FishAudio", "instance_name": "one", "model_name": "transcribe-1", "file": file})
		if out["code"] != float64(400) {
			t.Fatal("server path/invalid audio accepted", raw)
		}
	}
	if calls.Load() != count {
		t.Fatal("denied/invalid request made provider call")
	}
	for _, provider := range []string{"MiniMax", "FishAudio"} {
		model := "speech-2.8-hd"
		if provider == "FishAudio" {
			model = "s2-pro"
		}
		p := speechPayload(provider, model, "one", "missing-final")
		p["stream"] = true
		_, raw := request("speech-owner", "/audio/speech", p)
		if !strings.Contains(raw, "event: error") || strings.Contains(raw, "event: done") {
			t.Fatal("truncated stream falsely completed", raw)
		}
	}
	binaryData, err := os.ReadFile(binary)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("candidate-cli-sha256 %x", sha256.Sum256(binaryData))
	work := t.TempDir()
	work, err = filepath.EvalSymlinks(work)
	if err != nil {
		t.Fatal(err)
	}
	audioPath := filepath.Join(work, "input.wav")
	os.WriteFile(audioPath, []byte("client audio bytes"), 0600)
	players := filepath.Join(work, "players")
	os.Mkdir(players, 0700)
	marker := filepath.Join(work, "played-audio")
	pidfile := filepath.Join(work, "player-pid")
	for _, player := range []string{"afplay", "mpv", "ffplay"} {
		script := "#!/bin/sh\nfor arg do input=$arg; done\n/bin/cp \"$input\" \"$SPEECH_TEST_AUDIO\"\nprintf '%s' $$ > \"$SPEECH_TEST_PID\"\n"
		if err := os.WriteFile(filepath.Join(players, player), []byte(script), 0700); err != nil {
			t.Fatal(err)
		}
	}
	parsed, _ := url.Parse(api.URL)
	runCLI := func(syntax string, success bool) []map[string]any {
		t.Helper()
		process := exec.CommandContext(t.Context(), binary, "-h", parsed.Host, "-t", tokens["speech-owner"], "-o", "json", syntax)
		process.Dir = work
		process.Env = append(os.Environ(), "PATH="+players, "SPEECH_TEST_AUDIO="+marker, "SPEECH_TEST_PID="+pidfile)
		var output bytes.Buffer
		process.Stdout = &output
		process.Stderr = &output
		if err := process.Start(); err != nil {
			t.Fatal(err)
		}
		t.Log("owned-cli-process-open", process.Process.Pid)
		err := process.Wait()
		t.Log("owned-cli-process-closed", process.Process.Pid, process.ProcessState.ExitCode())
		if (err == nil) != success {
			t.Fatalf("CLI success=%v err=%v output=%s", success, err, output.String())
		}
		if !success {
			return nil
		}
		var rows []map[string]any
		if err := json.Unmarshal(output.Bytes(), &rows); err != nil || len(rows) != 1 {
			t.Fatal("invalid CLI rows", output.String())
		}
		return rows
	}
	for _, provider := range []string{"MiniMax", "FishAudio", "OpenRouter"} {
		model, params := "speech-2.8-hd", `{"voice_setting":{"voice_id":"fixture-voice","speed":1},"audio_setting":{"sample_rate":32000,"format":"mp3"}}`
		if provider == "FishAudio" {
			model = "s2-pro"
			params = `{"reference_id":"fixture-voice"}`
		}
		if provider == "OpenRouter" {
			model = "openai/gpt-audio-mini"
			params = `{"voice":"fixture-voice"}`
		}
		for _, instance := range []string{"one", "two"} {
			syntax := fmt.Sprintf(`TTS WITH '%s@%s@%s' TEXT 'hello' PARAM '%s' FORMAT wav`, model, instance, provider, params)
			filesBefore, _ := os.ReadDir(work)
			rows := runCLI(syntax+";", true)
			filesAfter, _ := os.ReadDir(work)
			if len(filesBefore) != len(filesAfter) {
				t.Fatal("unrequested TTS wrote cwd output")
			}
			got, err := base64.StdEncoding.Strict().DecodeString(rows[0]["audio"].(string))
			if err != nil || string(got) != "speech-audio-"+provider+"-"+instance || rows[0]["format"] != "wav" {
				t.Fatal("CLI speech bytes/format lost")
			}
			if provider != "OpenRouter" {
				syntax = "STREAM " + syntax
			}
			rows = runCLI(syntax+` SAVE './saved' PLAY;`, true)
			saved, _ := rows[0]["saved_path"].(string)
			t.Log("owned-saved-audio", saved)
			bytes, err := os.ReadFile(saved)
			if err != nil || string(bytes) != string(got) || filepath.Dir(saved) != filepath.Join(work, "saved") {
				t.Fatal("CLI saved bytes/path lost", err)
			}
			played, err := os.ReadFile(marker)
			if err != nil || string(played) != string(got) {
				t.Fatal("PLAY did not use generated bytes", err)
			}
			playerPID, err := os.ReadFile(pidfile)
			if err != nil {
				t.Fatal(err)
			}
			t.Log("owned-player-process-closed", string(playerPID))
		}
	}
	for _, instance := range []string{"one", "two"} {
		rows := runCLI(fmt.Sprintf(`ASR WITH 'transcribe-1@%s@FishAudio' AUDIO '%s' PARAM '{"language":"en","ignore_timestamps":true}';`, instance, audioPath), true)
		if rows[0]["text"] != "transcribed-"+instance {
			t.Fatal("CLI ASR text lost")
		}
	}
	for _, syntax := range []string{
		`TTS WITH 'speech-2.8-hd@one@MiniMax' TEXT 'provider-error';`,
		`STREAM TTS WITH 'speech-2.8-hd@one@MiniMax' TEXT 'missing-final' PARAM '{"voice_setting":{"voice_id":"fixture-voice","speed":1},"audio_setting":{"sample_rate":32000}}' FORMAT wav;`,
		`STREAM TTS WITH 's2-pro@one@FishAudio' TEXT 'missing-final' PARAM '{"reference_id":"fixture-voice"}' FORMAT wav;`,
		`STREAM TTS WITH 'openai/gpt-audio-mini@one@OpenRouter' TEXT 'hello';`,
		fmt.Sprintf(`STREAM ASR WITH 'transcribe-1@one@FishAudio' AUDIO '%s';`, audioPath),
	} {
		runCLI(syntax, false)
	}
	// User-supplied API credentials cannot replace the SQL-selected instance key.
	key, region := "caller-key", "caller-region"
	cfg := &models.TTSConfig{Format: "wav", Params: map[string]any{"voice_setting": map[string]any{"voice_id": "fixture-voice", "speed": 1}, "audio_setting": map[string]any{"sample_rate": 32000}}}
	cfgBefore, _ := json.Marshal(cfg)
	if _, code, err := svc.AudioSpeech("MiniMax", "one", "speech-2.8-hd", "speech-owner", "hello", &models.APIConfig{APIKey: &key, Region: &region}, cfg); err != nil || code != 0 {
		t.Fatal("SQL binding failed", code, err)
	}
	cfgAfter, _ := json.Marshal(cfg)
	if !bytes.Equal(cfgBefore, cfgAfter) || key != "caller-key" || region != "caller-region" {
		t.Fatal("caller config mutated")
	}
	if after := snapshot(); after != before {
		t.Fatal("inference or denied request mutated persisted identity/provider/model state")
	}
	for _, s := range append(providers, api) {
		s.Close()
		endpoint, _ := url.Parse(s.URL)
		conn, err := net.DialTimeout("tcp", endpoint.Host, 200*time.Millisecond)
		if err == nil {
			conn.Close()
			t.Fatal("owned listener survived close")
		}
		t.Log("owned-listener-closed-readback", s.URL)
	}
	t.Log("real CLI/JWT/SQL/provider drivers passed; two keys per provider isolated; denied calls=0; SQL independent snapshot unchanged; MIME/audio/text/params/format/save/play verified; unsupported ASR/TTS streams return 501; no real supplier calls or audio device used")
}
