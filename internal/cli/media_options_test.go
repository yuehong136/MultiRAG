package cli

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
)

func speechCLI(server string) *CLI {
	parsed, _ := url.Parse(server)
	port, _ := strconv.Atoi(parsed.Port())
	client := newConsumerCLI("user")
	consumerHTTP(client).Host = parsed.Hostname()
	consumerHTTP(client).Port = port
	consumerHTTP(client).APIToken = consumerTokenPtr("controlled")
	return client
}
func TestMediaOptionParser(t *testing.T) {
	cmd, err := NewParser(`STREAM TTS WITH 'model@instance@provider' TEXT 'hello' PARAM '{"audio_setting":{"sample_rate":32000}}' FORMAT wav PLAY SAVE './output';`).Parse(APIMode)
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
		if _, err := NewParser(syntax).Parse(APIMode); err != nil {
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
		if _, err := NewParser(syntax).Parse(APIMode); err == nil {
			t.Fatal("invalid options accepted", syntax)
		}
	}
}
func TestCLITranscriptionParamsSendBytes(t *testing.T) {
	file := filepath.Join(t.TempDir(), "input.wav")
	os.WriteFile(file, []byte("client bytes"), 0600)
	srv := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload map[string]any
		json.NewDecoder(r.Body).Decode(&payload)
		config, _ := payload["asr_config"].(map[string]any)
		params, _ := config["params"].(map[string]any)
		if r.URL.Path != "/api/v1/audio/transcriptions" || payload["file"] != base64.StdEncoding.EncodeToString([]byte("client bytes")) || params["language"] != "en" || params["ignore_timestamps"] != true {
			t.Error("bytes or params lost")
		}
		if _, ok := payload["audio_file"]; ok {
			t.Error("sent server-local file path")
		}
		io.WriteString(w, `{"code":0,"data":{"text":"transcribed words"}}`)
	}))
	defer srv.Close()
	cmd, err := NewParser(fmt.Sprintf(`ASR WITH 'transcribe-1@voice@FishAudio' AUDIO '%s' PARAM '{"language":"en","ignore_timestamps":true}';`, file)).Parse(APIMode)
	if err != nil {
		t.Fatal(err)
	}
	response, err := speechCLI(srv.URL).ExecuteCommand(cmd)
	if err != nil || response.(*CommonResponse).Data[0]["text"] != "transcribed words" {
		t.Fatal(response, err)
	}
}
func TestCLIInvalidParamsZeroHTTPAndPlayerFailure(t *testing.T) {
	var calls atomic.Int32
	srv := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		io.WriteString(w, `{"code":0,"data":{"audio":"eA==","format":"mp3"}}`)
	}))
	defer srv.Close()
	client := speechCLI(srv.URL)
	for _, option := range []string{`PARAM 'null'`, `PARAM '[]'`, `PARAM '{bad}'`, `PARAM '{"model":"bypass"}'`, `FORMAT '../bad'`} {
		cmd, err := NewParser(`TTS WITH 'a@b@c' TEXT 'hello' ` + option + `;`).Parse(APIMode)
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
	for _, name := range []string{"aplay"} {
		os.WriteFile(filepath.Join(players, name), []byte("#!/bin/sh\nexit 9\n"), 0700)
	}
	cmd, _ := NewParser(`TTS WITH 'a@b@c' TEXT 'hello' PLAY;`).Parse(APIMode)
	if _, err := client.ExecuteCommand(cmd); err == nil || !strings.Contains(err.Error(), "play speech") {
		t.Fatal("failed player reported success", err)
	}
}

func TestDefaultSpeechSaveFormat(t *testing.T) {
	srv := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, `{"code":0,"data":{"audio":"bXAzLWJ5dGVz","format":"mp3"}}`)
	}))
	defer srv.Close()
	dir := t.TempDir()
	cmd, err := NewParser(fmt.Sprintf(`TTS WITH 'openai/gpt-audio-mini@voice@OpenRouter' TEXT 'hello' SAVE '%s';`, dir)).Parse(APIMode)
	if err != nil {
		t.Fatal(err)
	}
	result, err := speechCLI(srv.URL).ExecuteCommand(cmd)
	if err != nil {
		t.Fatal(err)
	}
	if result == nil {
		t.Fatal("missing speech acknowledgement")
	}
	path := filepath.Join(dir, "openai_gpt-audio-mini_output.mp3")
	data, err := os.ReadFile(path)
	if err != nil || filepath.Ext(path) != ".mp3" || string(data) != "mp3-bytes" {
		t.Fatal("default format file extension/bytes lost", err)
	}
}
