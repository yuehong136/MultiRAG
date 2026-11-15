package cli

import (
	"io"
	"net/http"
	"net/url"
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
		cmd, err := NewParser(kind + ` WITH "model@instance@provider" ` + input + ` "value";`).Parse(APIMode)
		if err != nil {
			t.Fatal(kind, err)
		}
		if strings.HasPrefix(kind, "STREAM") && cmd.Params["stream"] != true {
			t.Fatal("stream flag missing")
		}
	}
	for _, input := range []string{`TTS WITH "a@b@c" TEXT "x" extra;`, `OCR WITH "a@b@c" FILE;`, `STREAM OCR WITH "a@b@c" FILE "x";`, `ASR AUDIO "x.wav";`, `TTS WITH "a@b@c" FILE "x";`} {
		if _, err := NewParser(input).Parse(APIMode); err == nil {
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
		c := newConsumerCLI("user")
		consumerHTTP(c).Host = u.Hostname()
		consumerHTTP(c).Port = port
		consumerHTTP(c).APIToken = consumerTokenPtr("controlled")
		cmd, _ := NewParser(`TTS WITH "a@b@c" TEXT "hello";`).Parse(APIMode)
		if _, err := c.ExecuteCommand(cmd); err == nil {
			t.Error("false success", raw)
		}
		server.Close()
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
	client := newConsumerCLI("user")
	consumerHTTP(client).Host = u.Hostname()
	consumerHTTP(client).Port = port
	consumerHTTP(client).APIToken = consumerTokenPtr("fixture")
	cmd, err := NewParser(`OCR WITH "HunyuanOCR@instance@Gitee" URL "https://example.com/image.png";`).Parse(APIMode)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.ExecuteCommand(cmd); err != nil {
		t.Fatal(err)
	}
	for _, input := range []string{`OCR WITH "a@b@c" URL "https://example.com/x" FILE "x";`, `OCR WITH "a@b@c" URL;`, `OCR WITH "a@b@c" URL "https://example.com/x" extra;`} {
		if _, err := NewParser(input).Parse(APIMode); err == nil {
			t.Fatal("invalid grammar accepted")
		}
	}
}
