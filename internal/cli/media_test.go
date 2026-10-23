package cli

import (
	"encoding/base64"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

func TestMediaParserContract(t *testing.T) {
	for _, kind := range []string{"ASR", "TTS", "OCR", "STREAM ASR", "STREAM TTS"} {
		input := "AUDIO"
		if strings.Contains(kind, "TTS") {
			input = "TEXT"
		}
		if kind == "OCR" {
			input = "FILE"
		}
		cmd, err := NewParser(kind + ` WITH "model@instance@provider" ` + input + ` "value";`).Parse(false)
		if err != nil {
			t.Fatal(kind, err)
		}
		if strings.HasPrefix(kind, "STREAM") && cmd.Params["stream"] != true {
			t.Fatal("stream flag missing")
		}
	}
	for _, input := range []string{`ASR WITH "a@b@c" AUDIO "a.wav"`, `TTS WITH "a@b@c" TEXT "x" extra;`, `OCR WITH "a@b@c" FILE;`, `STREAM OCR WITH "a@b@c" FILE "x";`, `ASR AUDIO "x.wav";`, `TTS WITH "a@b@c" FILE "x";`} {
		if _, err := NewParser(input).Parse(false); err == nil {
			t.Error("accepted invalid grammar", input)
		}
	}
	for _, word := range []string{"ASR", "TTS", "OCR"} {
		if !isKeyword(NewLexer(word).NextToken().Type) {
			t.Error("keyword range excludes", word)
		}
	}
}
func TestMediaCLIRejectsFailedOrMalformedResponse(t *testing.T) {
	for _, raw := range []string{`{}`, `{"code":501,"message":"unsupported"}`, `{"code":0,"data":{}}`, `{"code":0,"data":null}`, `{"code":"0","data":{"text":"x"}}`, `not json`, `{"code":0,"data":{"audio":[1,2],"format":"wav"}}`} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, raw) }))
		u, _ := url.Parse(server.URL)
		port, _ := strconv.Atoi(u.Port())
		c := NewMultiRAGClient("user")
		c.HTTPClient.Host = u.Hostname()
		c.HTTPClient.Port = port
		c.HTTPClient.APIKey = "controlled"
		cmd, _ := NewParser(`TTS WITH "a@b@c" TEXT "hello";`).Parse(false)
		if _, err := c.ExecuteCommand(cmd); err == nil {
			t.Error("false success", raw)
		}
		server.Close()
	}
}
func TestMediaStreamContract(t *testing.T) {
	good := "event: message\ndata: {\"text\":\"first\"}\n\nevent: message\ndata: {\"text\":\" second\"}\n\nevent: done\ndata: [DONE]\n\n"
	response, err := readMediaStream(strings.NewReader(good), "asr")
	if err != nil || response.(*CommonDataResponse).Data["text"] != "first second" {
		t.Fatal(response, err)
	}
	for _, body := range []string{"", `{"code":501}`, "event: done\ndata: [DONE]\n\n", strings.Replace(good, "event: done\ndata: [DONE]\n\n", "", 1), good + "event: error\ndata: {\"code\":500,\"message\":\"late failure\"}\n\n", "event: message\ndata: {}\n\nevent: done\ndata: [DONE]\n\n", "event: error\ndata: {\"code\":501,\"message\":\"unsupported\"}\n\n"} {
		if _, err := readMediaStream(strings.NewReader(body), "asr"); err == nil {
			t.Error("stream falsely completed", body)
		}
	}
}
func TestChatAudioFilePayload(t *testing.T) {
	path := filepath.Join(t.TempDir(), "speech.wav")
	data := []byte("controlled audio bytes")
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	cmd, err := NewParser(fmt.Sprintf(`CHAT WITH "model@instance@provider" MESSAGE "transcribe" AUDIO %q;`, path)).Parse(false)
	if err != nil {
		t.Fatal(err)
	}
	parts, err := chatContent("", cmd.Params["content_inputs"])
	if err != nil {
		t.Fatal(err)
	}
	values := parts.([]map[string]any)
	audio := values[1]["input_audio"].(map[string]any)
	if audio["format"] != "wav" || audio["data"] != base64.StdEncoding.EncodeToString(data) {
		t.Fatal("audio bytes changed", audio)
	}
	if _, err := chatContent("", []chatInput{{Kind: "input_audio", Value: path}, {Kind: "input_audio", Value: path}}); err == nil {
		t.Fatal("multiple audio accepted")
	}
	large := filepath.Join(t.TempDir(), "large.wav")
	file, err := os.Create(large)
	if err != nil {
		t.Fatal(err)
	}
	if err := file.Truncate(20*1024*1024 + 1); err != nil {
		t.Fatal(err)
	}
	file.Close()
	if _, err := readMediaFile(large); err == nil {
		t.Fatal("oversized file accepted")
	}
}
