package handler_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/binary"
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
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/utility"
)

// Provider HTTP is controlled; the CLI binary, registered JWT routes, factories,
// tenant/instance binding, PostgreSQL commits and independent reads are real.
func TestAfter775ProvidersHTTPCLIAndSQL(t *testing.T) {
	config, cliBinary := os.Getenv("MULTIRAG_AFTER775_D_CONFIG"), os.Getenv("MULTIRAG_AFTER775_D_CLI")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned after775 D PostgreSQL config required")
		}
		t.Skip("requires owned after775 D PostgreSQL config and candidate CLI")
	}
	if cliBinary == "" {
		t.Fatal("candidate CLI required")
	}
	projectRoot := filepath.Join("..", "..")
	if _, err := os.Stat("go.mod"); err == nil {
		projectRoot = "."
	}
	t.Chdir(projectRoot)
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal("private config load failed")
	}
	if db := server.GetConfig().Database; db.Database != "multirag_after775_d_20261025" || db.Host != "127.0.0.1" {
		t.Fatal("unowned database")
	}
	if err := dao.InitDB(); err != nil {
		t.Fatal("scratch database initialization failed", err)
	}
	pool, err := dao.DB.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close() })
	dsn, err := dao.PostgresDSN(server.GetConfig().Database)
	if err != nil {
		t.Fatal(err)
	}
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { readback.Close() })
	tables := []string{"tenant_model", "tenant_model_instance", "tenant_model_provider", "t_ai_user_tenants", "t_ai_users"}
	t.Cleanup(func() {
		for _, table := range tables {
			if _, err := readback.Exec("DELETE FROM " + table); err != nil {
				t.Error("owned SQL cleanup failed", table, err)
			}
			var count int
			if err := readback.QueryRow("SELECT count(*) FROM " + table).Scan(&count); err != nil || count != 0 {
				t.Error("owned SQL cleanup readback", table, count, err)
			}
		}
		t.Log("owned PostgreSQL cleanup independently read back zero rows in five tables")
	})
	if err := server.InitVariables(nil); err != nil {
		t.Fatal(err)
	}
	local.InitAdminStatus(0, "owned after775 provider acceptance")
	active := "1"
	tokens := map[string]string{}
	for _, id := range []string{"after775-d-owner", "after775-d-other"} {
		email := id + "@owned.invalid"
		if err := dao.DB.Create(&entity.User{ID: id, Email: email, Nickname: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := dao.DB.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: id + "-tenant", Role: "owner", InvitedBy: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		token, err := utility.SignUserJWT(email, server.GetSecretKey())
		if err != nil {
			t.Fatal(err)
		}
		tokens[id] = token
	}
	gin.SetMode(gin.ReleaseMode)
	app := gin.New()
	app.Use(gin.Recovery())
	svc := service.NewModelProviderService()
	routes := router.NewRouter(handler.NewAuthHandler(), nil, nil, nil, nil, handler.NewSystemHandler(service.NewSystemService()), nil, nil, nil, nil, nil, nil, nil, nil, nil, handler.NewProviderHandler(nil, svc), nil)
	routes.Setup(app)
	api := after775Server(t, "application", app)

	parsedAPI, _ := url.Parse(api.URL)
	client := &http.Client{Timeout: 15 * time.Second}
	request := func(method, path, identity string, body any) (int, map[string]any, []byte) {
		t.Helper()
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		req, err := http.NewRequestWithContext(t.Context(), method, api.URL+path, bytes.NewReader(raw))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Content-Type", "application/json")
		if identity != "" {
			req.Header.Set("Authorization", "Bearer "+tokens[identity])
		}
		resp, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		data, err := io.ReadAll(io.LimitReader(resp.Body, 25*1024*1024))
		if err != nil {
			t.Fatal(err)
		}
		var envelope map[string]any
		if !strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream") {
			if err := json.Unmarshal(data, &envelope); err != nil {
				t.Fatal("invalid application response", string(data))
			}
		}
		if bytes.Contains(data, []byte("private-provider-body")) {
			t.Fatal("provider body leaked through API")
		}
		return resp.StatusCode, envelope, data
	}
	success := func(method, path string, body any) map[string]any {
		t.Helper()
		status, envelope, raw := request(method, path, "after775-d-owner", body)
		if status != http.StatusOK || envelope["code"] != float64(0) {
			t.Fatalf("%s %s failed: %d %s", method, path, status, raw)
		}
		return envelope
	}
	snapshot := func() string {
		t.Helper()
		var result string
		for _, table := range tables {
			var value string
			if err := readback.QueryRow("SELECT md5(COALESCE(jsonb_agg(to_jsonb(t) ORDER BY id)::text,'')) FROM " + table + " t").Scan(&value); err != nil {
				t.Fatal("independent SQL snapshot", err)
			}
			result += value
		}
		return result
	}
	before := ""
	runCLI := func(t *testing.T, identity, syntax string, valid bool, want string) []byte {
		t.Helper()
		ctx, cancel := context.WithTimeout(t.Context(), 20*time.Second)
		defer cancel()
		command := exec.CommandContext(ctx, cliBinary, "-f", filepath.Join(filepath.Dir(config), "cli.json"), "-h", parsedAPI.Host, "-t", tokens[identity], "-o", "json", syntax)
		output, err := command.CombinedOutput()
		if command.ProcessState != nil {
			t.Log("owned-cli-process", command.Process.Pid, "exit", command.ProcessState.ExitCode(), syntax)
		}
		if valid != (err == nil) || want != "" && !bytes.Contains(output, []byte(want)) || bytes.Contains(output, []byte("private-provider-body")) || !valid && bytes.Contains(output, []byte("[DONE]")) {
			t.Fatalf("CLI expected success %v: %s: %v %s", valid, syntax, err, output)
		}
		if before != "" && snapshot() != before {
			t.Fatal("inference or denial mutated persisted rows")
		}
		return output
	}
	cliBytes, err := os.ReadFile(cliBinary)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("candidate CLI SHA256 %x", sha256.Sum256(cliBytes))
	var providerCalls atomic.Int64
	audio := make([]byte, 46)
	copy(audio, "RIFF")
	binary.LittleEndian.PutUint32(audio[4:], 38)
	copy(audio[8:], "WAVEfmt ")
	binary.LittleEndian.PutUint32(audio[16:], 16)
	binary.LittleEndian.PutUint16(audio[20:], 1)
	binary.LittleEndian.PutUint16(audio[22:], 1)
	binary.LittleEndian.PutUint32(audio[24:], 16000)
	binary.LittleEndian.PutUint32(audio[28:], 32000)
	binary.LittleEndian.PutUint16(audio[32:], 2)
	binary.LittleEndian.PutUint16(audio[34:], 16)
	copy(audio[36:], "data")
	binary.LittleEndian.PutUint32(audio[40:], 2)
	mediaDir := filepath.Join(filepath.Dir(config), "media")
	if err := os.Mkdir(mediaDir, 0700); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(mediaDir); err != nil {
			t.Error(err)
		}
		if _, err := os.Stat(mediaDir); !os.IsNotExist(err) {
			t.Error("media path remains")
		}
	})
	audioFile := filepath.Join(mediaDir, "owned.wav")
	if err := os.WriteFile(audioFile, audio, 0600); err != nil {
		t.Fatal(err)
	}
	type providerCase struct {
		name, chat, rerank, asr, tts string
		speechStream                 bool
	}
	providers := []providerCase{
		{name: "PPIO", chat: "deepseek/deepseek-r1"},
		{name: "TokenPony", chat: "qwen3-8b"},
		{name: "Groq", chat: "llama-3.1-8b-instant"},
		{name: "TogetherAI", chat: "openai/gpt-oss-20b", rerank: "mixedbread-ai/mxbai-rerank-large-v2", asr: "openai/whisper-large-v3", tts: "canopylabs/orpheus-3b-0.1-ft", speechStream: true},
		{name: "ZHIPU-AI", chat: "glm-4-plus", asr: "glm-asr-2512", tts: "glm-tts", speechStream: true},
		{name: "OpenRouter", chat: "google/gemma-4-31b-it", asr: "openai/whisper-large-v3", tts: "openai/gpt-audio-mini"},
	}
	for _, p := range providers {
		success(http.MethodPut, "/api/v1/providers/", map[string]any{"provider_name": p.name})
		definition := dao.GetModelProviderManager().FindProvider(p.name)
		if definition == nil {
			t.Fatal("catalog missing", p.name)
		}
		for _, instance := range []string{"one", "two"} {
			key := "controlled-" + p.name + "-" + instance
			fixture := after775Server(t, p.name+"/"+instance, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				providerCalls.Add(1)
				path := strings.TrimPrefix(r.URL.Path, "/instance/")
				if r.Header.Get("Authorization") != "Bearer "+key {
					t.Error("credentials crossed instances", p.name, instance)
				}
				if r.Method == http.MethodGet {
					if p.name == "OpenRouter" && path == definition.URLSuffix.Balance {
						fmt.Fprint(w, `{"data":{"total_credits":5,"total_usage":0}}`)
						return
					}
					if p.name == "ZHIPU-AI" && path == definition.URLSuffix.Files {
						fmt.Fprint(w, `{"data":[]}`)
						return
					}
					if path != definition.URLSuffix.Models || r.ContentLength > 0 {
						t.Error("discovery route/body lost", path)
					}
					if p.name == "TogetherAI" {
						json.NewEncoder(w).Encode([]map[string]any{{"id": p.chat}})
					} else {
						json.NewEncoder(w).Encode(map[string]any{"data": []map[string]any{{"id": p.chat}}})
					}
					return
				}
				if p.asr != "" && path == definition.URLSuffix.ASR && p.name != "OpenRouter" {
					if err := r.ParseMultipartForm(1024); err != nil {
						t.Error(err)
						return
					}
					defer r.MultipartForm.RemoveAll()
					f, _, err := r.FormFile("file")
					if err != nil {
						t.Error(err)
						return
					}
					defer f.Close()
					raw, _ := io.ReadAll(f)
					if !bytes.Equal(raw, audio) || r.FormValue("model") != p.asr || r.FormValue("language") != "en" {
						t.Error("multipart ASR binding lost")
					}
					if p.name == "ZHIPU-AI" && (r.FormValue("stream") != "false" || len(r.MultipartForm.Value["hotwords"]) != 2) {
						t.Error("Zhipu repeated fields lost")
					}
					if r.FormValue("prompt") == "bad" {
						fmt.Fprint(w, `{"text":null}`)
					} else {
						fmt.Fprintf(w, `{"text":%q}`, "recognized-"+instance)
					}
					return
				}
				var b map[string]any
				if err := json.NewDecoder(r.Body).Decode(&b); err != nil {
					t.Error(err)
					return
				}
				switch {
				case p.asr != "" && path == definition.URLSuffix.ASR:
					a := b["input_audio"].(map[string]any)
					if a["data"] != base64.StdEncoding.EncodeToString(audio) || a["format"] != "wav" || b["model"] != p.asr || b["language"] != "en" {
						t.Error("OpenRouter JSON ASR lost", b)
					}
					if b["prompt"] == "bad" {
						fmt.Fprint(w, `{"text":null}`)
					} else {
						fmt.Fprintf(w, `{"text":%q}`, "recognized-"+instance)
					}
				case p.tts != "" && path == definition.URLSuffix.TTS:
					if b["model"] != p.tts || b["response_format"] != "wav" || b["voice"] != "alloy" {
						t.Error("TTS model/options lost", b)
					}
					if b["input"] == "bad" {
						w.Header().Set("Content-Type", "application/json")
						fmt.Fprint(w, `{"error":"private-provider-body"}`)
						return
					}
					if p.name == "TogetherAI" && b["stream"] == true {
						w.Header().Set("Content-Type", "text/event-stream")
						fmt.Fprintf(w, "data: {\"type\":\"conversation.item.audio_output.delta\",\"delta\":%q}\n\n", base64.StdEncoding.EncodeToString(audio))
						if b["input"] != "truncated" {
							fmt.Fprint(w, "data: [DONE]\n\n")
						}
					} else {
						w.Header().Set("Content-Type", "audio/wav")
						w.Write(audio)
					}
				case p.rerank != "" && path == definition.URLSuffix.Rerank:
					if b["model"] != p.rerank || b["top_n"] != float64(2) {
						t.Error("rerank identity/options lost")
					}
					if b["query"] == "bad" {
						fmt.Fprint(w, `{"results":[{"index":0,"relevance_score":null}]}`)
					} else {
						fmt.Fprint(w, `{"results":[{"index":1,"relevance_score":0},{"index":0,"relevance_score":0.9}]}`)
					}
				default:
					if path != definition.URLSuffix.Chat || b["model"] != p.chat {
						t.Error("chat route/identity lost", path, b)
					}
					messages := b["messages"].([]any)
					prompt := fmt.Sprint(messages[len(messages)-1].(map[string]any)["content"])
					answer := "answer-" + p.name + "-" + instance
					if strings.Contains(prompt, "bad") {
						if b["stream"] == true {
							w.Header().Set("Content-Type", "text/event-stream")
							fmt.Fprint(w, "data: invalid\n\n")
						} else {
							w.WriteHeader(503)
							fmt.Fprint(w, "private-provider-body")
						}
						return
					}
					if b["stream"] == true {
						w.Header().Set("Content-Type", "text/event-stream")
						fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":%q},\"finish_reason\":\"stop\"}]}\n\n", answer)
					} else {
						json.NewEncoder(w).Encode(map[string]any{"choices": []map[string]any{{"message": map[string]any{"content": answer}}}})
					}
				}
			}))
			base := fixture.URL + "/instance"
			runCLI(t, "after775-d-owner", fmt.Sprintf(`CREATE PROVIDER %q INSTANCE %q KEY %q URL %q REGION 'office';`, p.name, instance, key, base), true, "")
			var stored, extra string
			if err := readback.QueryRow(`SELECT i.api_key,i.extra FROM tenant_model_instance i JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.provider_name=$1 AND i.instance_name=$2 AND p.tenant_id='after775-d-owner-tenant'`, p.name, instance).Scan(&stored, &extra); err != nil || stored != key || !strings.Contains(extra, base) {
				t.Fatal("committed instance readback", p.name, instance, err)
			}
		}
	}
	before = snapshot()
	for _, p := range providers {
		for _, instance := range []string{"one", "two"} {
			t.Run(p.name+"/"+instance, func(t *testing.T) {
				composite := func(name string) string { return name + "@" + instance + "@" + p.name }
				for _, prefix := range []string{"", "STREAM "} {
					runCLI(t, "after775-d-owner", fmt.Sprintf(`%sCHAT WITH %q MESSAGE 'q';`, prefix, composite(p.chat)), true, "answer-"+p.name+"-"+instance)
					runCLI(t, "after775-d-owner", fmt.Sprintf(`%sCHAT WITH %q MESSAGE 'bad';`, prefix, composite(p.chat)), false, "")
				}
				runCLI(t, "after775-d-owner", fmt.Sprintf(`LIST SUPPORTED MODELS FROM %q %q;`, p.name, instance), true, p.chat)
				runCLI(t, "after775-d-owner", fmt.Sprintf(`CHECK INSTANCE %q FROM %q;`, instance, p.name), true, "")
				if p.rerank != "" {
					b := map[string]any{"provider_name": p.name, "instance_name": instance, "model_name": p.rerank, "query": "q", "documents": []string{"a", "b"}}
					rows := success(http.MethodPost, "/api/v1/rerank", b)["data"].([]any)
					if len(rows) != 2 || rows[1].(map[string]any)["relevance_score"] != float64(0) {
						t.Fatal("legal zero lost")
					}
					b["query"] = "bad"
					_, e, _ := request(http.MethodPost, "/api/v1/rerank", "after775-d-owner", b)
					if e["code"] == float64(0) {
						t.Fatal("malformed rerank succeeded")
					}
				}
				if p.asr != "" {
					runCLI(t, "after775-d-owner", fmt.Sprintf(`ASR WITH %q AUDIO %q PARAM '{"language":"en","hotwords":["one","two"]}';`, composite(p.asr), audioFile), true, "recognized-"+instance)
					b := map[string]any{"provider_name": p.name, "instance_name": instance, "model_name": p.asr, "file": base64.StdEncoding.EncodeToString(audio), "format": "wav", "language": []string{"en"}, "asr_config": map[string]any{"params": map[string]any{"hotwords": []string{"one", "two"}}}}
					if text := success(http.MethodPost, "/api/v1/audio/transcriptions", b)["data"].(map[string]any)["text"]; text != "recognized-"+instance {
						t.Fatal("ASR response lost")
					}
					b["prompt"] = "bad"
					_, e, _ := request(http.MethodPost, "/api/v1/audio/transcriptions", "after775-d-owner", b)
					if e["code"] == float64(0) {
						t.Fatal("missing ASR text succeeded")
					}
					b["prompt"] = ""
					b["stream"] = true
					_, _, raw := request(http.MethodPost, "/api/v1/audio/transcriptions", "after775-d-owner", b)
					if bytes.Contains(raw, []byte("[DONE]")) || !bytes.Contains(raw, []byte("error")) {
						t.Fatal("unsupported ASR stream success")
					}
				}
				if p.tts != "" {
					raw := runCLI(t, "after775-d-owner", fmt.Sprintf(`TTS WITH %q TEXT 'hello' PARAM '{"voice":"alloy"}' FORMAT wav SAVE %q;`, composite(p.tts), mediaDir), true, "")
					var rows []map[string]any
					if err := json.Unmarshal(raw, &rows); err != nil || len(rows) != 1 {
						t.Fatal("CLI speech result", err)
					}
					decoded, err := base64.StdEncoding.Strict().DecodeString(rows[0]["audio"].(string))
					if err != nil || !bytes.Equal(decoded, audio) || rows[0]["format"] != "wav" {
						t.Fatal("speech bytes/format lost", err)
					}
					saved := rows[0]["saved_path"].(string)
					if filepath.Dir(saved) != mediaDir {
						t.Fatal("save outside owned directory")
					}
					stored, err := os.ReadFile(saved)
					if err != nil || !bytes.Equal(stored, audio) {
						t.Fatal("saved audio differs", err)
					}
					if err := os.Remove(saved); err != nil {
						t.Fatal(err)
					}
					runCLI(t, "after775-d-owner", fmt.Sprintf(`TTS WITH %q TEXT 'bad' PARAM '{"voice":"alloy"}' FORMAT wav;`, composite(p.tts)), false, "")
					runCLI(t, "after775-d-owner", fmt.Sprintf(`STREAM TTS WITH %q TEXT 'hello' PARAM '{"voice":"alloy"}' FORMAT wav;`, composite(p.tts)), p.speechStream, "")
					if p.speechStream {
						b := map[string]any{"provider_name": p.name, "instance_name": instance, "model_name": p.tts, "text": "hello", "voice": "alloy", "format": "wav", "stream": true}
						_, _, raw := request(http.MethodPost, "/api/v1/audio/speech", "after775-d-owner", b)
						if !bytes.Contains(raw, []byte(base64.StdEncoding.EncodeToString(audio))) || !bytes.Contains(raw, []byte("[DONE]")) {
							t.Fatal("HTTP audio stream bytes/terminal lost", string(raw))
						}
						if p.name == "TogetherAI" {
							b["text"] = "truncated"
							_, _, raw = request(http.MethodPost, "/api/v1/audio/speech", "after775-d-owner", b)
							if bytes.Contains(raw, []byte("[DONE]")) || !bytes.Contains(raw, []byte("error")) {
								t.Fatal("truncated audio stream success")
							}
						}
					}
				}
				n := providerCalls.Load()
				runCLI(t, "after775-d-other", fmt.Sprintf(`CHAT WITH %q MESSAGE 'q';`, composite(p.chat)), false, "")
				_, e, _ := request(http.MethodPost, "/api/v1/chat/completions", "", map[string]any{"provider_name": p.name, "instance_name": instance, "model_name": p.chat, "message": "q"})
				if e["code"] == float64(0) || providerCalls.Load() != n || snapshot() != before {
					t.Fatal("auth denial side effects")
				}
				if p.asr != "" {
					n = providerCalls.Load()
					_, e, _ = request(http.MethodPost, "/api/v1/audio/transcriptions", "after775-d-other", map[string]any{"provider_name": p.name, "instance_name": instance, "model_name": p.asr, "file": base64.StdEncoding.EncodeToString(audio)})
					if e["code"] == float64(0) || providerCalls.Load() != n || snapshot() != before {
						t.Fatal("ASR tenant denial side effects")
					}
				}
				if snapshot() != before {
					t.Fatal("inference changed SQL")
				}
			})
		}
	}
	if _, err := readback.Exec(`UPDATE tenant_model_instance SET status='disabled' WHERE id IN (SELECT i.id FROM tenant_model_instance i JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.provider_name='PPIO' AND i.instance_name='one')`); err != nil {
		t.Fatal(err)
	}
	before = snapshot()
	n := providerCalls.Load()
	runCLI(t, "after775-d-owner", `CHAT WITH 'deepseek/deepseek-r1@one@PPIO' MESSAGE 'q';`, false, "")
	for _, b := range []map[string]any{{"provider_name": "TogetherAI", "instance_name": "one", "model_name": "openai/gpt-oss-20b", "file": base64.StdEncoding.EncodeToString(audio)}, {"provider_name": "Groq", "instance_name": "one", "model_name": "llama-3.1-8b-instant", "file": base64.StdEncoding.EncodeToString(audio)}} {
		_, e, _ := request(http.MethodPost, "/api/v1/audio/transcriptions", "after775-d-owner", b)
		if e["code"] == float64(0) || providerCalls.Load() != n || snapshot() != before {
			t.Fatal("wrong capability side effects")
		}
	}
	_, e, _ := request(http.MethodPost, "/api/v1/providers/PPIO/instances", "after775-d-owner", map[string]any{"instance_name": "missing-key", "api_key": "", "base_url": api.URL})
	if e["code"] == float64(0) || providerCalls.Load() != n || snapshot() != before {
		t.Fatal("missing key side effects")
	}
	t.Log("six providers/twelve SQL instances, real JWT/HTTP/CLI and independent SQL snapshots; controlled provider requests", providerCalls.Load())
}

func after775Server(t *testing.T, label string, h http.Handler) *httptest.Server {
	t.Helper()
	registry := os.Getenv("MULTIRAG_AFTER775_D_RESOURCES")
	if registry == "" {
		t.Fatal("owned resource registry required")
	}
	record := func(value map[string]any) {
		t.Helper()
		f, err := os.OpenFile(registry, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
		if err != nil {
			t.Fatal(err)
		}
		defer f.Close()
		if err := json.NewEncoder(f).Encode(value); err != nil {
			t.Fatal(err)
		}
	}
	record(map[string]any{"action": "intent", "label": label, "listen": "127.0.0.1:0"})
	srv := httptest.NewUnstartedServer(h)
	address := srv.Listener.Addr().String()
	record(map[string]any{"action": "bound", "label": label, "address": address})
	srv.Start()
	t.Cleanup(func() {
		srv.CloseClientConnections()
		srv.Close()
		conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
		if err == nil {
			conn.Close()
			t.Error("listener remains", address)
		}
		record(map[string]any{"action": "closed-readback", "label": label, "address": address, "absent": err != nil})
	})
	return srv
}
