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
	"multirag/internal/entity/models"
	"multirag/internal/handler"
	"multirag/internal/router"
	"multirag/internal/server"
	"multirag/internal/server/local"
	"multirag/internal/service"
	"multirag/internal/utility"
)

// Provider HTTP is controlled; the CLI binary, registered JWT routes, factories,
// tenant/instance binding, PostgreSQL commits and independent reads are real.
func TestAfterF9ProvidersHTTPCLIAndSQL(t *testing.T) {
	config, cliBinary := os.Getenv("MULTIRAG_AFTERF9_I_CONFIG"), os.Getenv("MULTIRAG_AFTERF9_I_CLI")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned afterf9 I PostgreSQL config required")
		}
		t.Skip("requires owned afterf9 I PostgreSQL config and candidate CLI")
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
	if db := server.GetConfig().Database; db.Database != "multirag_afterf9_i_20261025" || db.Host != "127.0.0.1" {
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
	local.InitAdminStatus(0, "owned afterf9 provider acceptance")
	active := "1"
	tokens := map[string]string{}
	for _, id := range []string{"afterf9-i-owner", "afterf9-i-other"} {
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
	api := afterf9Server(t, "application", app)

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
		status, envelope, raw := request(method, path, "afterf9-i-owner", body)
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
	afterf9Record(t, map[string]any{"action": "directory-intent", "path": mediaDir})
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
	type providerCase struct{ name, chat, embedding, rerank, asr, tts, ocr string }
	providers := []providerCase{
		{name: "Astraflow", chat: "claude-opus-4-7", embedding: "text-embedding-3-large", rerank: "bge-reranker-v2-m3", tts: "IndexTeam/IndexTTS-2"},
		{name: "Groq", chat: "openai/gpt-oss-20b", asr: "whisper-large-v3", tts: "canopylabs/orpheus-v1-english"},
		{name: "OpenAI", asr: "whisper-1", tts: "tts-1"},
		{name: "HunYuan", chat: "hunyuan-pro", embedding: "hunyuan-embedding"},
		{name: "TokenHub", chat: "gpt-4o-mini", embedding: "fixture-embedding"},
		{name: "PaddleOCR_Local", ocr: "PaddleOCR-VL"},
		{name: "ZHIPU-AI", ocr: "glm-ocr"},
	}
	image := []byte("%PDF-1.7\nowned OCR fixture")
	imageFile := filepath.Join(mediaDir, "owned.pdf")
	if err := os.WriteFile(imageFile, image, 0600); err != nil {
		t.Fatal(err)
	}
	for _, p := range providers {
		success(http.MethodPut, "/api/v1/providers/", map[string]any{"provider_name": p.name})
		definition := dao.GetModelProviderManager().FindProvider(p.name)
		if definition == nil {
			t.Fatal("catalog missing", p.name)
		}
		for _, instance := range []string{"one", "two"} {
			key := "controlled-" + p.name + "-" + instance
			if p.name == "PaddleOCR_Local" {
				key = ""
			}
			fixture := afterf9Server(t, p.name+"/"+instance, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				providerCalls.Add(1)
				wantAuth := ""
				if key != "" {
					wantAuth = "Bearer " + key
				}
				if r.Header.Get("Authorization") != wantAuth {
					t.Error("credentials crossed instances", p.name, instance)
				}
				path := strings.TrimPrefix(r.URL.Path, "/instance/")
				w.Header().Set("Content-Type", "application/json")
				if r.Method == http.MethodGet {
					if path != definition.URLSuffix.Models {
						t.Error("discovery suffix lost", path)
					}
					fmt.Fprintf(w, `{"data":[{"id":%q}]}`, p.chat)
					return
				}
				if p.asr != "" && path == definition.URLSuffix.ASR {
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
						t.Error("ASR multipart contract lost")
					}
					if r.FormValue("prompt") == "bad" {
						fmt.Fprint(w, `{"text":null}`)
						return
					}
					if r.FormValue("stream") == "true" {
						w.Header().Set("Content-Type", "text/event-stream")
						fmt.Fprintf(w, "data: {\"type\":\"transcript.text.delta\",\"delta\":%q}\n\n", "recognized-"+instance)
						if r.FormValue("prompt") != "truncated" {
							fmt.Fprint(w, "data: {\"type\":\"transcript.text.done\"}\n\n")
						}
						return
					}
					fmt.Fprintf(w, `{"text":%q}`, "recognized-"+instance)
					return
				}
				var b map[string]any
				if err := json.NewDecoder(r.Body).Decode(&b); err != nil {
					t.Error(err)
					return
				}
				switch {
				case p.embedding != "" && path == definition.URLSuffix.Embedding:
					if b["model"] != p.embedding {
						t.Error("embedding model lost")
					}
					texts := b["input"].([]any)
					if texts[0] == "bad" {
						fmt.Fprint(w, `{"data":[{"index":0,"embedding":[null,1]}]}`)
						return
					}
					if len(texts) != 2 {
						t.Error("embedding input count")
					}
					fmt.Fprint(w, `{"data":[{"index":1,"embedding":[0,1]},{"index":0,"embedding":[1,0]}]}`)
				case p.rerank != "" && path == definition.URLSuffix.Rerank:
					if b["model"] != p.rerank {
						t.Error("rerank model lost")
					}
					if b["query"] == "bad" {
						fmt.Fprint(w, `{"results":[{"index":0,"relevance_score":null}]}`)
						return
					}
					fmt.Fprint(w, `{"results":[{"index":1,"relevance_score":0},{"index":0,"relevance_score":0.9}]}`)
				case p.tts != "" && path == definition.URLSuffix.TTS:
					if b["model"] != p.tts || b["voice"] != "alloy" {
						t.Error("speech identity/options lost")
					}
					format := b["response_format"]
					if p.name == "Astraflow" {
						format = b["format"]
					}
					if format != "wav" {
						t.Error("speech format lost")
					}
					if b["input"] == "bad" {
						fmt.Fprint(w, `{"error":"private-provider-body"}`)
						return
					}
					if b["stream_format"] == "sse" {
						w.Header().Set("Content-Type", "text/event-stream")
						fmt.Fprintf(w, "data: {\"type\":\"speech.audio.delta\",\"audio\":%q}\n\n", base64.StdEncoding.EncodeToString(audio))
						if b["input"] != "truncated" {
							fmt.Fprint(w, "data: {\"type\":\"speech.audio.done\"}\n\n")
						}
						return
					}
					w.Header().Set("Content-Type", "audio/wav")
					w.Write(audio)
				case p.ocr != "" && path == definition.URLSuffix.OCR:
					if p.name == "ZHIPU-AI" {
						if b["model"] != p.ocr || (b["file"] != "data:application/pdf;base64,"+base64.StdEncoding.EncodeToString(image) && b["file"] != "https://example.com/owned.pdf") {
							t.Error("Zhipu OCR input lost")
						}
						fmt.Fprintf(w, `{"md_results":%q}`, "page-"+instance)
					} else {
						if b["file"] != base64.StdEncoding.EncodeToString(image) || b["fileType"] != float64(0) {
							t.Error("authorized OCR bytes lost")
						}
						fmt.Fprintf(w, `{"errorCode":0,"result":{"layoutParsingResults":[{"markdown":{"text":%q}}]}}`, "page-"+instance)
					}
				default:
					if p.chat == "" || path != definition.URLSuffix.Chat || b["model"] != p.chat {
						t.Error("chat identity/suffix lost", path)
						return
					}
					messages := b["messages"].([]any)
					content := messages[len(messages)-1].(map[string]any)["content"]
					prompt, ok := content.(string)
					if !ok {
						parts, valid := content.([]any)
						if !valid {
							t.Error("chat text parts missing")
							return
						}
						for _, part := range parts {
							value, valid := part.(map[string]any)
							if !valid || value["type"] != "text" {
								t.Error("unexpected chat content part")
								return
							}
							text, valid := value["text"].(string)
							if !valid {
								t.Error("chat part text missing")
								return
							}
							prompt += text
						}
					}

					if prompt == "zero-options" && (b["temperature"] != float64(0) || b["top_p"] != float64(0)) {
						t.Error("explicit zero generation options lost")
					}
					if prompt == "bad" {
						fmt.Fprint(w, `{"choices":[]}`)
						return
					}
					answer := "answer-" + p.name + "-" + instance
					if b["stream"] == true {
						w.Header().Set("Content-Type", "text/event-stream")
						fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":%q},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n", answer)
					} else {
						fmt.Fprintf(w, `{"choices":[{"message":{"content":%q}}]}`, answer)
					}
				}
			}))
			base := fixture.URL + "/instance"
			runCLI(t, "afterf9-i-owner", fmt.Sprintf(`CREATE PROVIDER %q INSTANCE %q KEY %q URL %q REGION 'office';`, p.name, instance, key, base), true, "")
			if p.name == "TokenHub" {
				success(http.MethodPost, "/api/v1/providers/TokenHub/instances/"+instance+"/models", map[string]any{"model_name": p.embedding, "model_type": "embedding", "max_tokens": 1024})
			}
			var stored, extra string
			if err := readback.QueryRow(`SELECT i.api_key,i.extra FROM tenant_model_instance i JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.provider_name=$1 AND i.instance_name=$2 AND p.tenant_id='afterf9-i-owner-tenant'`, p.name, instance).Scan(&stored, &extra); err != nil || stored != key || !strings.Contains(extra, base) {
				t.Fatal("instance SQL readback", err)
			}
		}
	}
	before = snapshot()
	for _, p := range providers {
		for _, instance := range []string{"one", "two"} {
			t.Run(p.name+"/"+instance, func(t *testing.T) {
				composite := func(name string) string { return name + "@" + instance + "@" + p.name }
				identity := func(name string) map[string]any {
					return map[string]any{"provider_name": p.name, "instance_name": instance, "model_name": name}
				}
				deny := func(path string, b map[string]any, user string) {
					t.Helper()
					n := providerCalls.Load()
					_, e, raw := request(http.MethodPost, path, user, b)
					if e["code"] == float64(0) || providerCalls.Load() != n || snapshot() != before {
						t.Fatalf("denial side effects: %s %s", path, raw)
					}
				}
				if p.chat != "" {
					for _, prefix := range []string{"", "STREAM "} {
						runCLI(t, "afterf9-i-owner", fmt.Sprintf(`%sCHAT WITH %q MESSAGE 'q';`, prefix, composite(p.chat)), true, "answer-"+p.name+"-"+instance)
						runCLI(t, "afterf9-i-owner", fmt.Sprintf(`%sCHAT WITH %q MESSAGE 'bad';`, prefix, composite(p.chat)), false, "")
					}
					runCLI(t, "afterf9-i-owner", fmt.Sprintf(`LIST SUPPORTED MODELS FROM %q %q;`, p.name, instance), true, p.chat)
					b := identity(p.chat)
					b["messages"] = []map[string]any{{"role": "user", "content": "q"}}
					success(http.MethodPost, "/api/v1/chat/completions", b)
					// The public diagnostic chat route exposes thinking/stream, not numeric
					// generation fields. Exercise numeric zeros through its real SQL-bound facade.
					zero := 0.0
					_, code, err := svc.ChatToModelWithMessages(p.name, instance, p.chat, "afterf9-i-owner", []models.Message{{Role: "user", Content: "zero-options"}}, &models.APIConfig{Context: t.Context()}, &models.ChatConfig{Temperature: &zero, TopP: &zero})
					if err != nil || code != common.CodeSuccess {
						t.Fatal("SQL-bound zero generation failed", err)
					}

					deny("/api/v1/chat/completions", b, "afterf9-i-other")
					deny("/api/v1/chat/completions", b, "")
				}
				if p.embedding != "" {
					raw := runCLI(t, "afterf9-i-owner", fmt.Sprintf(`EMBED TEXT 'a' 'b' WITH %q DIMENSION 2;`, composite(p.embedding)), true, "")
					if !bytes.Contains(raw, []byte(`"index":0`)) && !bytes.Contains(raw, []byte(`"index": 0`)) {
						t.Fatal("indexed CLI embedding lost", string(raw))
					}
					b := identity(p.embedding)
					b["texts"] = []string{"a", "b"}
					b["dimension"] = 2
					rows := success(http.MethodPost, "/api/v1/embeddings", b)["data"].([]any)
					if len(rows) != 2 || rows[0].(map[string]any)["index"] != float64(0) || rows[0].(map[string]any)["embedding"].([]any)[1] != float64(0) {
						t.Fatal("embedding order or legal zero lost", rows)
					}
					deny("/api/v1/embeddings", b, "afterf9-i-other")
					b["texts"] = []string{"bad"}
					_, e, _ := request(http.MethodPost, "/api/v1/embeddings", "afterf9-i-owner", b)
					if e["code"] == float64(0) {
						t.Fatal("invalid raw embedding succeeded")
					}
				}
				if p.rerank != "" {
					b := identity(p.rerank)
					b["query"] = "q"
					b["documents"] = []string{"a", "b"}
					rows := success(http.MethodPost, "/api/v1/rerank", b)["data"].([]any)
					if len(rows) != 2 || rows[1].(map[string]any)["relevance_score"] != float64(0) {
						t.Fatal("rerank zero lost")
					}
					deny("/api/v1/rerank", b, "afterf9-i-other")
				}
				if p.asr != "" {
					runCLI(t, "afterf9-i-owner", fmt.Sprintf(`ASR WITH %q AUDIO %q PARAM '{"language":"en"}';`, composite(p.asr), audioFile), true, "recognized-"+instance)
					b := identity(p.asr)
					b["file"] = base64.StdEncoding.EncodeToString(audio)
					b["format"] = "wav"
					b["language"] = []string{"en"}
					success(http.MethodPost, "/api/v1/audio/transcriptions", b)
					deny("/api/v1/audio/transcriptions", b, "afterf9-i-other")
					b["stream"] = true
					_, _, raw := request(http.MethodPost, "/api/v1/audio/transcriptions", "afterf9-i-owner", b)
					if p.name == "OpenAI" {
						if !bytes.Contains(raw, []byte("[DONE]")) || !bytes.Contains(raw, []byte("recognized-"+instance)) {
							t.Fatal("OpenAI ASR stream lost", string(raw))
						}
						b["prompt"] = "truncated"
						_, _, raw = request(http.MethodPost, "/api/v1/audio/transcriptions", "afterf9-i-owner", b)
						if bytes.Contains(raw, []byte("[DONE]")) || !bytes.Contains(raw, []byte("error")) {
							t.Fatal("truncated ASR succeeded", string(raw))
						}
					} else if bytes.Contains(raw, []byte("[DONE]")) {
						t.Fatal("unsupported Groq ASR stream succeeded")
					}
				}
				if p.tts != "" {
					raw := runCLI(t, "afterf9-i-owner", fmt.Sprintf(`TTS WITH %q TEXT 'hello' PARAM '{"voice":"alloy"}' FORMAT wav SAVE %q;`, composite(p.tts), mediaDir), true, "")
					var rows []map[string]any
					if err := json.Unmarshal(raw, &rows); err != nil || len(rows) != 1 {
						t.Fatal("speech CLI result", err)
					}
					saved := rows[0]["saved_path"].(string)
					stored, err := os.ReadFile(saved)
					if filepath.Dir(saved) != mediaDir || err != nil || !bytes.Equal(stored, audio) {
						t.Fatal("saved speech bytes differ", err)
					}
					if err := os.Remove(saved); err != nil {
						t.Fatal(err)
					}
					b := identity(p.tts)
					b["text"] = "hello"
					b["voice"] = "alloy"
					b["format"] = "wav"
					success(http.MethodPost, "/api/v1/audio/speech", b)
					deny("/api/v1/audio/speech", b, "afterf9-i-other")
					if p.name == "OpenAI" {
						b["stream"] = true
						b["tts_config"] = map[string]any{"params": map[string]any{"stream_format": "sse"}}
						_, _, raw := request(http.MethodPost, "/api/v1/audio/speech", "afterf9-i-owner", b)
						if !bytes.Contains(raw, []byte("[DONE]")) || !bytes.Contains(raw, []byte(base64.StdEncoding.EncodeToString(audio))) {
							t.Fatal("speech SSE lost", string(raw))
						}
						b["text"] = "truncated"
						_, _, raw = request(http.MethodPost, "/api/v1/audio/speech", "afterf9-i-owner", b)
						if bytes.Contains(raw, []byte("[DONE]")) || !bytes.Contains(raw, []byte("error")) {
							t.Fatal("truncated speech succeeded")
						}
					}
					runCLI(t, "afterf9-i-owner", fmt.Sprintf(`TTS WITH %q TEXT 'bad' PARAM '{"voice":"alloy"}' FORMAT wav SAVE %q;`, composite(p.tts), mediaDir), false, "")
				}
				if p.ocr != "" {
					runCLI(t, "afterf9-i-owner", fmt.Sprintf(`OCR WITH %q FILE %q;`, composite(p.ocr), imageFile), true, "page-"+instance)
					b := identity(p.ocr)
					b["content"] = base64.StdEncoding.EncodeToString(image)
					success(http.MethodPost, "/api/v1/file/ocr", b)
					deny("/api/v1/file/ocr", b, "afterf9-i-other")
					delete(b, "content")
					b["url"] = "https://example.com/owned.pdf"
					if p.name == "PaddleOCR_Local" {
						deny("/api/v1/file/ocr", b, "afterf9-i-owner")
					} else {
						success(http.MethodPost, "/api/v1/file/ocr", b)
					}
				}
				if snapshot() != before {
					t.Fatal("inference changed SQL")
				}
			})
		}
	}
	t.Run("capability-and-disabled-denials", func(t *testing.T) {
		for _, test := range []struct {
			path string
			body map[string]any
		}{
			{"/api/v1/audio/transcriptions", map[string]any{"provider_name": "Groq", "instance_name": "one", "model_name": "openai/gpt-oss-20b", "file": base64.StdEncoding.EncodeToString(audio)}},
			{"/api/v1/embeddings", map[string]any{"provider_name": "HunYuan", "instance_name": "one", "model_name": "hunyuan-pro", "texts": []string{"a", "b"}}},
			{"/api/v1/file/ocr", map[string]any{"provider_name": "OpenAI", "instance_name": "one", "model_name": "tts-1", "content": base64.StdEncoding.EncodeToString(image)}},
		} {
			n := providerCalls.Load()
			_, e, _ := request(http.MethodPost, test.path, "afterf9-i-owner", test.body)
			if e["code"] == float64(0) || providerCalls.Load() != n || snapshot() != before {
				t.Fatal("capability denial side effects", test.path)
			}
		}
		if _, err := readback.Exec(`UPDATE tenant_model_instance SET status='disabled' WHERE id IN (SELECT i.id FROM tenant_model_instance i JOIN tenant_model_provider p ON p.id=i.provider_id WHERE p.provider_name='TokenHub' AND i.instance_name='one')`); err != nil {
			t.Fatal(err)
		}
		before = snapshot()
		n := providerCalls.Load()
		runCLI(t, "afterf9-i-owner", `CHAT WITH 'gpt-4o-mini@one@TokenHub' MESSAGE 'q';`, false, "")
		if providerCalls.Load() != n {
			t.Fatal("disabled instance called provider")
		}
	})
	n := providerCalls.Load()
	_, e, _ := request(http.MethodPost, "/api/v1/providers/HunYuan/instances", "afterf9-i-owner", map[string]any{"instance_name": "missing-key", "api_key": "", "base_url": api.URL})
	if e["code"] == float64(0) || snapshot() != before || providerCalls.Load() != n {
		t.Fatal("hosted missing-key side effects")
	}
	entries, err := os.ReadDir(mediaDir)
	if err != nil || len(entries) != 2 {
		t.Fatal("inference or failure left staged files", entries, err)
	}
	t.Log("seven providers/fourteen instances, JWT/HTTP/CLI, independent SQL and controlled provider requests", providerCalls.Load())
}

func afterf9Record(t *testing.T, value map[string]any) {
	t.Helper()
	registry := os.Getenv("MULTIRAG_AFTERF9_I_RESOURCES")
	if registry == "" {
		t.Fatal("owned resource registry required")
	}
	f, err := os.OpenFile(registry, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if err := json.NewEncoder(f).Encode(value); err != nil {
		t.Fatal(err)
	}
}
func afterf9Server(t *testing.T, label string, h http.Handler) *httptest.Server {
	t.Helper()
	afterf9Record(t, map[string]any{"action": "intent", "label": label, "listen": "127.0.0.1:0"})
	srv := httptest.NewUnstartedServer(h)
	address := srv.Listener.Addr().String()
	afterf9Record(t, map[string]any{"action": "bound", "label": label, "address": address})
	srv.Start()
	t.Cleanup(func() {
		srv.CloseClientConnections()
		srv.Close()
		conn, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
		if err == nil {
			conn.Close()
			t.Error("listener remains", address)
		}
		afterf9Record(t, map[string]any{"action": "closed-readback", "label": label, "address": address, "absent": err != nil})
	})
	return srv
}
