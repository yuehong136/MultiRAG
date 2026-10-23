package cli

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
)

func speechCLI(server string) *MultiRAGClient {
	parsed, _ := url.Parse(server)
	port, _ := strconv.Atoi(parsed.Port())
	client := NewMultiRAGClient("user")
	client.HTTPClient.Host = parsed.Hostname()
	client.HTTPClient.Port = port
	client.HTTPClient.APIKey = "controlled"
	return client
}
func TestMediaOptionParser(t *testing.T) {
	cmd, err := NewParser(`STREAM TTS WITH 'model@instance@provider' TEXT 'hello' PARAM '{"audio_setting":{"sample_rate":32000}}' FORMAT wav PLAY SAVE './output';`).Parse(false)
	if err != nil {
		t.Fatal(err)
	}
	if cmd.Params["stream"] != true || cmd.Params["format"] != "wav" || cmd.Params["play"] != true || cmd.Params["save"] != true || cmd.Params["save_path"] != "./output" || cmd.Params["param_str"] != `{"audio_setting":{"sample_rate":32000}}` {
		t.Fatal("option after PARAM swallowed", cmd.Params)
	}
	for _, word := range []string{"PARAM", "FORMAT", "PLAY", "SAVE"} {
		if !isKeyword(NewLexer(word).NextToken().Type) {
			t.Fatal("keyword excluded", word)
		}
	}
	for _, syntax := range []string{
		`TTS WITH 'a@b@c' TEXT 'x' PARAM '{}' PLAY FORMAT wav SAVE 'out';`,
		`TTS WITH 'a@b@c' TEXT 'x' SAVE 'out' FORMAT 'wav' PARAM '{}' PLAY;`,
		`ASR WITH 'a@b@c' AUDIO 'x.wav' PARAM '{"language":"en"}';`,
	} {
		if _, err := NewParser(syntax).Parse(false); err != nil {
			t.Fatal(syntax, err)
		}
	}
	for _, syntax := range []string{
		`TTS WITH 'a@b@c' TEXT 'x' PARAM '{}' junk;`,
		`TTS WITH 'a@b@c' TEXT 'x' PARAM`,
		`TTS WITH 'a@b@c' TEXT 'x' PARAM {};`,
		`TTS WITH 'a@b@c' TEXT 'x' SAVE;`,
		`TTS WITH 'a@b@c' TEXT 'x' FORMAT;`,
		`TTS WITH 'a@b@c' TEXT 'x' PLAY PLAY;`,
		`TTS WITH 'a@b@c' TEXT 'x' FORMAT wav FORMAT mp3;`,
		`ASR WITH 'a@b@c' AUDIO 'x.wav' PLAY;`,
		`OCR WITH 'a@b@c' FILE 'x' PARAM '{}';`,
	} {
		if _, err := NewParser(syntax).Parse(false); err == nil {
			t.Fatal("invalid options accepted", syntax)
		}
	}
}
func TestCLITranscriptionParamsSendBytes(t *testing.T) {
	file := filepath.Join(t.TempDir(), "input.wav")
	os.WriteFile(file, []byte("client bytes"), 0600)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload map[string]any
		json.NewDecoder(r.Body).Decode(&payload)
		config, _ := payload["asr_config"].(map[string]any)
		params, _ := config["params"].(map[string]any)
		if r.URL.Path != "/api/v1/audio/transcriptions" || payload["file"] != base64.StdEncoding.EncodeToString([]byte("client bytes")) || params["language"] != "en" || params["ignore_timestamps"] != true || config["format"] != "wav" {
			t.Error("bytes or params lost")
		}
		if _, ok := payload["audio_file"]; ok {
			t.Error("sent server-local file path")
		}
		io.WriteString(w, `{"code":0,"data":{"text":"transcribed words"}}`)
	}))
	defer srv.Close()
	cmd, err := NewParser(fmt.Sprintf(`ASR WITH 'transcribe-1@voice@FishAudio' AUDIO '%s' PARAM '{"language":"en","ignore_timestamps":true}';`, file)).Parse(false)
	if err != nil {
		t.Fatal(err)
	}
	response, err := speechCLI(srv.URL).ExecuteCommand(cmd)
	if err != nil || response.(*CommonDataResponse).Data["text"] != "transcribed words" {
		t.Fatal(response, err)
	}
}
func TestCLISpeechSavePlayAndNoDefaultFiles(t *testing.T) {
	work := t.TempDir()
	t.Chdir(work)
	players := t.TempDir()
	marker := filepath.Join(work, "played")
	t.Setenv("PATH", players)
	t.Setenv("SPEECH_TEST_MARKER", marker)
	// The player receives the actual saved audio; no host audio device is involved.
	for _, name := range []string{"afplay", "mpv", "ffplay"} {
		if err := os.WriteFile(filepath.Join(players, name), []byte("#!/bin/sh\nfor arg do input=$arg; done\n/bin/cp \"$input\" \"$SPEECH_TEST_MARKER\"\n"), 0700); err != nil {
			t.Fatal(err)
		}
	}
	audio := []byte("wave bytes")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload map[string]any
		json.NewDecoder(r.Body).Decode(&payload)
		cfg, _ := payload["tts_config"].(map[string]any)
		params, _ := cfg["params"].(map[string]any)
		if payload["model_name"] != "openai/gpt-audio-mini" || cfg["format"] != "wav" || params["voice"] != "fixture" {
			t.Error("speech config lost")
		}
		if payload["stream"] == true {
			w.Header().Set("Content-Type", "text/event-stream")
			fmt.Fprintf(w, "event: message\ndata: {\"audio\":%q,\"format\":\"wav\"}\n\nevent: done\ndata: [DONE]\n\n", base64.StdEncoding.EncodeToString(audio))
		} else {
			json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": map[string]any{"audio": audio, "format": "wav"}})
		}
	}))
	defer srv.Close()
	client := speechCLI(srv.URL)
	base := `TTS WITH 'openai/gpt-audio-mini@voice@OpenRouter' TEXT 'hello' PARAM '{"voice":"fixture"}' FORMAT wav`
	run := func(syntax string) *CommonDataResponse {
		t.Helper()
		cmd, err := NewParser(syntax).Parse(false)
		if err != nil {
			t.Fatal(err)
		}
		response, err := client.ExecuteCommand(cmd)
		if err != nil {
			t.Fatal(err)
		}
		return response.(*CommonDataResponse)
	}
	run(base + ";")
	entries, _ := os.ReadDir(work)
	if len(entries) != 0 {
		t.Fatal("default TTS wrote files")
	}
	row := run("STREAM " + base + ` SAVE 'nested/output' PLAY;`)
	saved, _ := row.Data["saved_path"].(string)
	if !filepath.IsAbs(saved) || filepath.Dir(saved) != filepath.Join(work, "nested/output") || strings.Contains(filepath.Base(saved), "/") || filepath.Ext(saved) != ".wav" {
		t.Fatal("unsafe/wrong saved path", saved)
	}
	raw, err := os.ReadFile(saved)
	if err != nil || !bytes.Equal(raw, audio) {
		t.Fatal("saved bytes lost", err)
	}
	played, err := os.ReadFile(marker)
	if err != nil || !bytes.Equal(played, audio) {
		t.Fatal("player did not get saved audio", err)
	}
	// Repeated SAVE is independent and does not overwrite prior output.
	again := run(base + ` SAVE 'nested/output';`)
	if again.Data["saved_path"] == saved {
		t.Fatal("SAVE overwrote prior file")
	}
	run(base + ` PLAY;`)
	if _, err := os.Stat(marker); err != nil {
		t.Fatal("PLAY process not run")
	}
	binary, args, err := speechPlayer("linux", saved)
	if err != nil || filepath.Base(binary) != "mpv" || args[len(args)-1] != saved {
		t.Fatal("Linux player selection wrong", err)
	}
}
func TestCLIInvalidParamsZeroHTTPAndPlayerFailure(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		io.WriteString(w, `{"code":0,"data":{"audio":"eA==","format":"mp3"}}`)
	}))
	defer srv.Close()
	client := speechCLI(srv.URL)
	for _, option := range []string{`PARAM 'null'`, `PARAM '[]'`, `PARAM '{bad}'`, `PARAM '{"model":"bypass"}'`, `FORMAT '../bad'`} {
		cmd, err := NewParser(`TTS WITH 'a@b@c' TEXT 'hello' ` + option + `;`).Parse(false)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := client.ExecuteCommand(cmd); err == nil {
			t.Fatal("bad params succeeded")
		}
	}
	if calls.Load() != 0 {
		t.Fatal("invalid params made HTTP call")
	}
	players := t.TempDir()
	t.Setenv("PATH", players)
	for _, name := range []string{"afplay", "mpv", "ffplay"} {
		os.WriteFile(filepath.Join(players, name), []byte("#!/bin/sh\nexit 9\n"), 0700)
	}
	cmd, _ := NewParser(`TTS WITH 'a@b@c' TEXT 'hello' PLAY;`).Parse(false)
	if _, err := client.ExecuteCommand(cmd); err == nil || !strings.Contains(err.Error(), "play speech") {
		t.Fatal("failed player reported success", err)
	}
}

func TestDefaultSpeechSaveFormat(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, `{"code":0,"data":{"audio":"bXAzLWJ5dGVz","format":"mp3"}}`)
	}))
	defer srv.Close()
	dir := t.TempDir()
	cmd, err := NewParser(fmt.Sprintf(`TTS WITH 'openai/gpt-audio-mini@voice@OpenRouter' TEXT 'hello' SAVE '%s';`, dir)).Parse(false)
	if err != nil {
		t.Fatal(err)
	}
	result, err := speechCLI(srv.URL).ExecuteCommand(cmd)
	if err != nil {
		t.Fatal(err)
	}
	path, _ := result.(*CommonDataResponse).Data["saved_path"].(string)
	data, err := os.ReadFile(path)
	if err != nil || filepath.Ext(path) != ".mp3" || string(data) != "mp3-bytes" {
		t.Fatal("default format file extension/bytes lost", err)
	}
}
