package handler_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"multirag/internal/cli"
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

type controlledMediaFactory struct {
	t                  *testing.T
	constructed, calls atomic.Int32
	started, canceled  chan struct{}
}

func (f *controlledMediaFactory) CreateModelDriver(provider string, base map[string]string, suffix models.URLSuffix) (models.ModelDriver, error) {
	f.constructed.Add(1)
	return &controlledMediaDriver{DummyModel: models.NewDummyModel(base, suffix), factory: f, instance: filepath.Base(base["default"])}, nil
}

type controlledMediaDriver struct {
	*models.DummyModel
	factory  *controlledMediaFactory
	instance string
}

func (d *controlledMediaDriver) check(api *models.APIConfig) error {
	d.factory.calls.Add(1)
	if api == nil || api.APIKey == nil || *api.APIKey != "controlled-"+d.instance || api.Region == nil || *api.Region != "region-"+d.instance {
		d.factory.t.Error("trusted instance settings lost or crossed instances")
	}
	if api != nil && api.Context != nil {
		if deadline, ok := api.Context.Deadline(); !ok || time.Until(deadline) > 120*time.Second {
			d.factory.t.Error("missing bounded media context")
		}
		return api.Context.Err()
	}
	return nil
}
func (d *controlledMediaDriver) TranscribeAudio(_ *string, audio []byte, api *models.APIConfig, config *models.ASRConfig) (*models.ASRResponse, error) {
	if err := d.check(api); err != nil {
		return nil, err
	}
	if len(config.Language) > 0 {
		config.Language[0] = "changed"
		config.Prompt = "changed"
	}
	switch string(audio) {
	case "nil-result":
		return nil, nil
	case "empty-result":
		return &models.ASRResponse{}, nil
	case "wait":
		close(d.factory.started)
		<-api.Context.Done()
		close(d.factory.canceled)
		return nil, api.Context.Err()
	}
	return &models.ASRResponse{Text: "transcribed-" + d.instance}, nil
}
func (d *controlledMediaDriver) TranscribeAudioWithSender(_ *string, audio []byte, api *models.APIConfig, _ *models.ASRConfig, send func(*models.ASRResponse) error) error {
	if err := d.check(api); err != nil {
		return err
	}
	if string(audio) == "empty-stream" {
		return nil
	}
	if err := send(&models.ASRResponse{Text: "transcribed-"}); err != nil {
		return err
	}
	if string(audio) == "partial-error" {
		return errors.New("controlled stream failure")
	}
	return send(&models.ASRResponse{Text: d.instance})
}
func (d *controlledMediaDriver) AudioSpeech(_ *string, text string, api *models.APIConfig, _ *models.TTSConfig) (*models.TTSResponse, error) {
	if err := d.check(api); err != nil {
		return nil, err
	}
	if text == "nil-result" {
		return nil, nil
	}
	if text == "empty-result" {
		return &models.TTSResponse{}, nil
	}
	return &models.TTSResponse{Audio: []byte("controlled-audio-" + d.instance), Format: "wav"}, nil
}
func (d *controlledMediaDriver) AudioSpeechWithSender(_ *string, text string, api *models.APIConfig, _ *models.TTSConfig, send func(*models.TTSResponse) error) error {
	if err := d.check(api); err != nil {
		return err
	}
	if text == "empty-stream" {
		return nil
	}
	sendErr := send(&models.TTSResponse{Audio: []byte("controlled-audio-"), Format: "wav"})
	if text == "ignore-sender" {
		return nil
	}
	if sendErr != nil {
		return sendErr
	}
	if text == "partial-error" {
		return errors.New("controlled stream failure")
	}
	format := "wav"
	if text == "format-change" {
		format = "mp3"
	}
	return send(&models.TTSResponse{Audio: []byte(d.instance), Format: format})
}
func (d *controlledMediaDriver) OCRFile(_ *string, content []byte, api *models.APIConfig, _ *models.OCRConfig) (*models.OCRResponse, error) {
	if err := d.check(api); err != nil {
		return nil, err
	}
	if string(content) == "empty-result" {
		return &models.OCRResponse{}, nil
	}
	return &models.OCRResponse{Text: "recognized-" + d.instance}, nil
}

// Real router/JWT/SQL binding and CLI processes, with only the model driver controlled.
func TestMediaHTTPCLIAndSQL(t *testing.T) {
	config := os.Getenv("MULTIRAG_MEDIA_CONFIG")
	if config == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned media scratch config required")
		}
		t.Skip("requires owned media scratch database")
	}
	binary := os.Getenv("MULTIRAG_MEDIA_CLI")
	if binary == "" {
		t.Fatal("candidate CLI binary required")
	}
	t.Chdir(filepath.Join("..", ".."))
	if err := common.Init("error"); err != nil {
		t.Fatal(err)
	}
	if err := server.FromConfigFile(config); err != nil {
		t.Fatal("load owned config failed")
	}
	if server.GetConfig().Database.Database != "multirag_after9b_c_media" {
		t.Fatal("unowned media database")
	}
	if err := dao.InitDB(); err != nil {
		t.Fatal("initialize media scratch", err)
	}
	pool, _ := dao.DB.DB()
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
	local.InitAdminStatus(0, "owned media acceptance")
	active, inactive := "1", "0"
	tokens := map[string]string{}
	for _, user := range []string{"owner", "foreign", "revoked", "disabled"} {
		status := active
		if user == "disabled" {
			status = inactive
		}
		email := user + "@media.invalid"
		if err := dao.DB.Create(&entity.User{ID: user, Email: email, Nickname: user, Status: &status}).Error; err != nil {
			t.Fatal(err)
		}
		relationStatus := active
		if user == "revoked" {
			relationStatus = inactive
		}
		if err := dao.DB.Create(&entity.UserTenant{ID: user, UserID: user, TenantID: user + "-tenant", Role: "owner", InvitedBy: user, Status: &relationStatus}).Error; err != nil {
			t.Fatal(err)
		}
		token, err := utility.SignUserJWT(email, server.GetSecretKey())
		if err != nil {
			t.Fatal(err)
		}
		tokens[user] = token
	}
	if err := dao.DB.Create(&entity.TenantModelProvider{ID: "media-provider", TenantID: "owner-tenant", ProviderName: "ZHIPU-AI"}).Error; err != nil {
		t.Fatal(err)
	}
	for _, instance := range []string{"a", "b", "disabled"} {
		status := "active"
		if instance == "disabled" {
			status = "disabled"
		}
		extra, _ := json.Marshal(map[string]string{"region": "region-" + instance, "base_url": "http://127.0.0.1:1/" + instance})
		if err := dao.DB.Create(&entity.TenantModelInstance{ID: instance, ProviderID: "media-provider", InstanceName: instance, Status: status, APIKey: "controlled-" + instance, Extra: string(extra)}).Error; err != nil {
			t.Fatal(err)
		}
		for _, kind := range []string{"asr", "tts", "ocr", "chat", "disabled-asr"} {
			typ := kind
			if kind == "asr" || kind == "disabled-asr" {
				typ = "speech2text"
			}
			modelStatus := "active"
			if kind == "disabled-asr" {
				modelStatus = "disabled"
			}
			detail, _ := json.Marshal(map[string]any{"max_tokens": 8192, "model_types": []string{typ}})
			if err := dao.DB.Create(&entity.TenantModel{ID: instance + "-" + kind, ProviderID: "media-provider", InstanceID: instance, ModelName: kind, ModelType: typ, Status: modelStatus, Extra: string(detail)}).Error; err != nil {
				t.Fatal(err)
			}
		}
	}
	snapshot := func() string {
		t.Helper()
		var result string
		if err := readback.QueryRow(`SELECT (SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM tenant_model_provider x) || (SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM tenant_model_instance x) || (SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM tenant_model x) || (SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM t_ai_user_tenants x) || (SELECT jsonb_agg(to_jsonb(x) ORDER BY id)::text FROM t_ai_users x)`).Scan(&result); err != nil {
			t.Fatal(err)
		}
		return result
	}
	before := snapshot()
	factory := &controlledMediaFactory{t: t, started: make(chan struct{}), canceled: make(chan struct{})}
	svc := service.NewModelProviderServiceWithFactory(factory)
	gin.SetMode(gin.ReleaseMode)
	serve := func(svc *service.ModelProviderService) *httptest.Server {
		app := gin.New()
		app.Use(gin.Recovery())
		r := router.NewRouter(handler.NewAuthHandler(), nil, nil, nil, nil, handler.NewSystemHandler(service.NewSystemService()), nil, nil, nil, nil, nil, nil, nil, nil, nil, handler.NewProviderHandler(nil, svc), nil)
		r.Setup(app)
		api := httptest.NewServer(app)
		t.Cleanup(api.Close)
		t.Log("owned-http-resource", api.URL)
		return api
	}
	api := serve(svc)
	payload := func(kind, instance string) map[string]any {
		p := map[string]any{"provider_name": "ZHIPU-AI", "instance_name": instance, "model_name": kind}
		switch kind {
		case "asr":
			p["file"] = []byte("controlled-input")
			p["format"] = "wav"
		case "tts":
			p["text"] = "hello"
		case "ocr":
			p["content"] = []byte("controlled-input")
		}
		return p
	}
	paths := map[string]string{"asr": "/audio/transcriptions", "tts": "/audio/speech", "ocr": "/file/ocr"}
	call := func(base, kind, user string, body any) (int, map[string]any, string) {
		t.Helper()
		raw, _ := json.Marshal(body)
		req, err := http.NewRequest("POST", base+"/api/v1"+paths[kind], bytes.NewReader(raw))
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Content-Type", "application/json")
		if user != "" {
			req.Header.Set("Authorization", "Bearer "+tokens[user])
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
		var out map[string]any
		if !strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream") {
			if err := json.Unmarshal(data, &out); err != nil {
				t.Fatal(err)
			}
		}
		return resp.StatusCode, out, string(data)
	}
	for _, instance := range []string{"a", "b"} {
		for _, kind := range []string{"asr", "tts", "ocr"} {
			_, out, raw := call(api.URL, kind, "owner", payload(kind, instance))
			if out["code"] != float64(0) {
				t.Fatal("media inference failed", raw)
			}
			data := out["data"].(map[string]any)
			expected := "transcribed-" + instance
			if kind == "ocr" {
				expected = "recognized-" + instance
			}
			if kind == "tts" {
				audio, err := base64.StdEncoding.DecodeString(data["audio"].(string))
				if err != nil || string(audio) != "controlled-audio-"+instance || data["format"] != "wav" {
					t.Fatal("audio bytes/format lost")
				}
			} else if data["text"] != expected {
				t.Fatal("text lost", raw)
			}
		}
	}
	for _, kind := range []string{"asr", "tts"} {
		p := payload(kind, "a")
		p["stream"] = true
		_, _, raw := call(api.URL, kind, "owner", p)
		if !strings.Contains(raw, "event: done") || !strings.Contains(raw, "[DONE]") {
			t.Fatal("stream completion missing", raw)
		}
	}
	var parallel sync.WaitGroup
	for i := range 8 {
		parallel.Go(func() {
			instance := "a"
			if i%2 == 1 {
				instance = "b"
			}
			_, out, raw := call(api.URL, "tts", "owner", payload("tts", instance))
			if out["code"] != float64(0) {
				t.Error("concurrent instance failed", raw)
			}
		})
	}
	parallel.Wait()
	calls, constructed := factory.calls.Load(), factory.constructed.Load()
	for _, tc := range []struct{ user, model, instance string }{{"foreign", "asr", "a"}, {"revoked", "asr", "a"}, {"disabled", "asr", "a"}, {"", "asr", "a"}, {"owner", "chat", "a"}, {"owner", "disabled-asr", "a"}, {"owner", "asr", "disabled"}, {"owner", "missing", "a"}, {"owner", "asr", "missing"}} {
		p := payload("asr", tc.instance)
		p["model_name"] = tc.model
		_, out, raw := call(api.URL, "asr", tc.user, p)
		if out["code"] == float64(0) || out["data"] != nil {
			t.Fatal("unauthorized/wrong/disabled model accepted", raw)
		}
	}
	for _, p := range []map[string]any{{"file": "%%%"}, {"file": []int{1, 2}}, {"file": ""}, {"file": []byte("x"), "format": "ogg"}} {
		body := payload("asr", "a")
		for key, value := range p {
			body[key] = value
		}
		_, out, raw := call(api.URL, "asr", "owner", body)
		if out["code"] != float64(400) {
			t.Fatal("bad input accepted", raw)
		}
	}
	if factory.calls.Load() != calls || factory.constructed.Load() != constructed {
		t.Fatal("denial/invalid input constructed or called driver")
	}
	for _, kind := range []string{"asr", "tts", "ocr"} {
		p := payload(kind, "a")
		field := "file"
		var bad any = []byte("empty-result")
		if kind == "tts" {
			field = "text"
			bad = "empty-result"
		}
		if kind == "ocr" {
			field = "content"
		}
		p[field] = bad
		_, out, raw := call(api.URL, kind, "owner", p)
		if out["code"] != float64(500) || out["data"] != nil {
			t.Fatal("empty driver result accepted", raw)
		}
	}
	for _, kind := range []string{"asr", "tts"} {
		for _, failure := range []string{"partial-error", "empty-stream"} {
			p := payload(kind, "a")
			p["stream"] = true
			if kind == "tts" {
				p["text"] = failure
			} else {
				p["file"] = []byte(failure)
			}
			_, _, raw := call(api.URL, kind, "owner", p)
			if !strings.Contains(raw, "event: error") || strings.Contains(raw, "event: done") {
				t.Fatal("failed stream falsely completed", raw)
			}
		}
	}
	wrongFormat := payload("tts", "a")
	wrongFormat["format"] = "mp3"
	_, mismatch, mismatchRaw := call(api.URL, "tts", "owner", wrongFormat)
	if mismatch["code"] != float64(500) || mismatch["data"] != nil {
		t.Fatal("wrong audio format succeeded", mismatchRaw)
	}
	// A real configured driver with no implemented capability returns 501, without network probing.
	unsupported := serve(service.NewModelProviderService())
	for _, kind := range []string{"asr", "tts", "ocr"} {
		_, out, raw := call(unsupported.URL, kind, "owner", payload(kind, "a"))
		if out["code"] != float64(501) || out["data"] != nil {
			t.Fatal("unsupported capability falsely succeeded", raw)
		}
	}
	p := payload("asr", "a")
	p["stream"] = true
	_, _, raw := call(unsupported.URL, "asr", "owner", p)
	if !strings.Contains(raw, `"code":501`) || strings.Contains(raw, "event: done") {
		t.Fatal("unsupported stream falsely completed", raw)
	}
	// Caller credentials/config cannot override SQL; adapters receive isolated config copies.
	key, region := "caller-key", "caller-region"
	apiConfig := &models.APIConfig{APIKey: &key, Region: &region}
	cfg := &models.ASRConfig{Language: []string{"en"}, Prompt: "keep"}
	if _, code, err := svc.TranscribeAudio("ZHIPU-AI", "a", "asr", "owner", []byte("x"), apiConfig, cfg); err != nil || code != 0 {
		t.Fatal(err)
	}
	if cfg.Language[0] != "en" || cfg.Prompt != "keep" || key != "caller-key" || region != "caller-region" {
		t.Fatal("caller config mutated")
	}
	// Real HTTP cancellation reaches the adapter and completes without a background leak.
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	waitPayload := payload("asr", "a")
	waitPayload["file"] = []byte("wait")
	body, _ := json.Marshal(waitPayload)
	req, _ := http.NewRequestWithContext(ctx, "POST", api.URL+"/api/v1/audio/transcriptions", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+tokens["owner"])
	done := make(chan error, 1)
	go func() {
		resp, err := http.DefaultClient.Do(req)
		if resp != nil {
			resp.Body.Close()
		}
		done <- err
	}()
	select {
	case <-factory.started:
	case <-ctx.Done():
		t.Fatal("adapter not started")
	}
	cancel()
	select {
	case <-factory.canceled:
	case <-time.After(2 * time.Second):
		t.Fatal("cancellation did not reach adapter")
	}
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatal("HTTP cancel lost", err)
	}
	// Actual candidate CLI binary: success + business/stream failure exit codes.
	binaryData, err := os.ReadFile(binary)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("candidate-cli-sha256 %x", sha256.Sum256(binaryData))
	temp := t.TempDir()
	audioPath := filepath.Join(temp, "speech.wav")
	imagePath := filepath.Join(temp, "image.png")
	for _, path := range []string{audioPath, imagePath} {
		if err := os.WriteFile(path, []byte("controlled-input"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	parsed, _ := url.Parse(api.URL)
	port, _ := strconv.Atoi(parsed.Port())
	client := cli.NewMultiRAGClient("user")
	client.HTTPClient.Host = parsed.Hostname()
	client.HTTPClient.Port = port
	client.HTTPClient.LoginToken = tokens["owner"]
	for _, syntax := range []string{fmt.Sprintf(`ASR WITH "asr@a@ZHIPU-AI" AUDIO %q;`, audioPath), `TTS WITH "tts@b@ZHIPU-AI" TEXT "hello";`, fmt.Sprintf(`OCR WITH "ocr@a@ZHIPU-AI" FILE %q;`, imagePath), fmt.Sprintf(`STREAM ASR WITH "asr@b@ZHIPU-AI" AUDIO %q;`, audioPath), `STREAM TTS WITH "tts@a@ZHIPU-AI" TEXT "hello";`} {
		cmd, err := cli.NewParser(syntax).Parse(false)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := client.ExecuteCommand(cmd); err != nil {
			t.Fatal("CLI command failed", err)
		}
		process := exec.CommandContext(t.Context(), binary, "-h", parsed.Host, "-t", tokens["owner"], "-o", "json", syntax)
		process.Dir = temp
		output, err := process.CombinedOutput()
		if err != nil {
			t.Fatalf("candidate CLI process failed: %s", output)
		}
		if !bytes.Contains(output, []byte("transcribed-")) && !bytes.Contains(output, []byte("recognized-")) && !bytes.Contains(output, []byte("audio")) {
			t.Fatal("CLI output missing media result")
		}
	}
	for _, syntax := range []string{`TTS WITH "tts@a@ZHIPU-AI" TEXT "empty-result";`, `STREAM TTS WITH "tts@a@ZHIPU-AI" TEXT "partial-error";`, `ASR WITH "asr@missing@ZHIPU-AI" AUDIO "missing.wav";`} {
		process := exec.CommandContext(t.Context(), binary, "-h", parsed.Host, "-t", tokens["owner"], syntax)
		process.Dir = temp
		if output, err := process.CombinedOutput(); err == nil {
			t.Fatal("CLI failure exited zero", string(output))
		}
	}
	unsupportedURL, _ := url.Parse(unsupported.URL)
	process := exec.CommandContext(t.Context(), binary, "-h", unsupportedURL.Host, "-t", tokens["owner"], `STREAM TTS WITH "tts@a@ZHIPU-AI" TEXT "hello";`)
	process.Dir = temp
	if output, err := process.CombinedOutput(); err == nil || !bytes.Contains(output, []byte("501")) {
		t.Fatal("unsupported CLI stream exit lost", err, string(output))
	}
	// Even if an adapter ignores a failed sender and returns nil, success is forbidden.
	sendErr := errors.New("controlled sender closed")
	code, streamErr := svc.AudioSpeechStream("ZHIPU-AI", "a", "tts", "owner", "ignore-sender", nil, nil, func(*models.TTSResponse) error { return sendErr })
	if code == 0 || !errors.Is(streamErr, sendErr) {
		t.Fatal("sender failure lost", streamErr)
	}
	api.Close()
	unsupported.Close()
	for _, base := range []string{api.URL, unsupported.URL} {
		endpoint, _ := url.Parse(base)
		conn, err := net.DialTimeout("tcp", endpoint.Host, 200*time.Millisecond)
		if err == nil {
			conn.Close()
			t.Fatal("owned HTTP listener survived close")
		}
		t.Log("owned-http-closed-readback", base)
	}
	if snapshot() != before {
		t.Fatal("media inference or denied request mutated SQL state")
	}
	t.Log("real JWT/router/HTTP/CLI/SQL media contracts passed; two instance keys isolated; type/enabled/tenant/invalid denials zero driver calls; empty/unsupported/partial streams fail; context cancellation read back; SQL unchanged")
}
