package cli

import (
	"encoding/base64"
	"fmt"
	"io"
	"math"
	"multirag/internal/entity/models"
	"net/http"
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
		server := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, raw) }))
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

func TestMediaExplicitEmptyText(t *testing.T) {
	for _, kind := range []string{"asr", "ocr"} {
		row, err := mediaResult(kind, []byte(`{"text":""}`))
		if err != nil || row["text"] != "" {
			t.Fatal("explicit empty text rejected", row, err)
		}
		for _, raw := range []string{`{}`, `{"text":null}`} {
			if _, err := mediaResult(kind, []byte(raw)); err == nil {
				t.Fatal("missing text accepted", raw)
			}
		}
	}
	stream := "event: message\ndata: {\"text\":\"\"}\n\nevent: done\ndata: [DONE]\n\n"
	if _, err := readMediaStream(strings.NewReader(stream), "asr"); err != nil {
		t.Fatal("explicit silent stream rejected", err)
	}
}

func TestMediaResponseBufferBounds(t *testing.T) {
	server := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, strings.Repeat("x", 64)) }))
	defer server.Close()
	u, _ := url.Parse(server.URL)
	port, _ := strconv.Atoi(u.Port())
	client := NewHTTPClient()
	client.Host = u.Hostname()
	client.Port = port
	if _, err := client.RequestBounded("GET", "/media", "api", nil, nil, 32); err == nil {
		t.Fatal("oversized HTTP response buffered")
	}
	if _, err := client.RequestBounded("GET", "/media", "api", nil, nil, 0); err == nil {
		t.Fatal("nonpositive limit accepted")
	}
	if _, err := client.RequestBounded("GET", "/media", "api", nil, nil, math.MaxInt64); err == nil {
		t.Fatal("overflowing response limit accepted")
	}
	// Many small data lines must not evade the per-frame bound.
	body := "event: message\n" + strings.Repeat("data: xxxxxxxx\n", base64.StdEncoding.EncodedLen(models.MaxMediaBytes)/9+10000)
	if _, err := readMediaStream(strings.NewReader(body), "asr"); err == nil || !strings.Contains(err.Error(), "frame exceeds") {
		t.Fatal("unbounded multiline SSE frame", err)
	}
}

func TestOCRURLParserAndClient(t *testing.T) {
	srv := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		if r.URL.Path != "/api/v1/file/ocr" || !strings.Contains(string(raw), `"url":"https://example.com/image.png"`) || strings.Contains(string(raw), `"content"`) {
			t.Errorf("request %s %s", r.URL.Path, raw)
		}
		io.WriteString(w, `{"code":0,"data":{"text":"识别🙂"}}`)
	}))
	defer srv.Close()
	u, _ := url.Parse(srv.URL)
	port, _ := strconv.Atoi(u.Port())
	client := NewMultiRAGClient("user")
	client.HTTPClient.Host = u.Hostname()
	client.HTTPClient.Port = port
	client.HTTPClient.APIKey = "fixture"
	cmd, err := NewParser(`OCR WITH "HunyuanOCR@instance@Gitee" URL "https://example.com/image.png";`).Parse(false)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.ExecuteCommand(cmd); err != nil {
		t.Fatal(err)
	}
	for _, input := range []string{`OCR WITH "a@b@c" URL "https://example.com/x" FILE "x";`, `OCR WITH "a@b@c" URL;`, `OCR WITH "a@b@c" URL "https://example.com/x" extra;`} {
		if _, err := NewParser(input).Parse(false); err == nil {
			t.Fatal("invalid grammar accepted")
		}
	}
}
