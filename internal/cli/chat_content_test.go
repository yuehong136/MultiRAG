package cli

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"testing"
)

func TestChatMediaParserAndPayload(t *testing.T) {
	image := filepath.Join(t.TempDir(), "图片 one.png")
	data, _ := base64.StdEncoding.DecodeString("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aH9kAAAAASUVORK5CYII=")
	if err := os.WriteFile(image, data, 0600); err != nil {
		t.Fatal(err)
	}
	for _, stream := range []bool{false, true} {
		calls := 0
		server := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls++
			var body struct {
				Messages []struct {
					Role    string
					Content []map[string]any
				}
				Stream       bool
				Thinking     bool
				ProviderName string `json:"provider_name"`
				InstanceName string `json:"instance_name"`
				ModelName    string `json:"model_name"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatal(err)
			}
			if len(body.Messages) != 1 || body.Messages[0].Role != "user" || len(body.Messages[0].Content) != 6 || body.Stream != stream || !body.Thinking || body.ProviderName != "vllm" || body.InstanceName != "local" || body.ModelName != "vision" {
				t.Fatalf("payload %+v", body)
			}
			parts := body.Messages[0].Content
			for i, kind := range []string{"text", "text", "image_url", "image_file", "video_url", "file_url"} {
				if parts[i]["type"] != kind {
					t.Error("part order changed")
				}
			}
			if parts[3]["image_file"].(map[string]any)["content"] != base64.StdEncoding.EncodeToString(data) {
				t.Error("image MIME or bytes lost")
			}
			if stream {
				fmt.Fprint(w, "data: [MESSAGE]answer\n\ndata: [DONE]\n\n")
			} else {
				fmt.Fprint(w, `{"code":0,"answer":"answer"}`)
			}
		}))
		u, _ := url.Parse(server.URL)
		port, _ := strconv.Atoi(u.Port())
		client := newConsumerCLI("user")
		consumerHTTP(client).Host = u.Hostname()
		consumerHTTP(client).Port = port
		prefix := "THINK "
		if stream {
			prefix = "STREAM THINK "
		}
		input := prefix + fmt.Sprintf(`CHAT WITH "vision@local@vllm" MESSAGE "describe" "第二段" IMAGE "https://example.com/a.png" %q VIDEO "https://example.com/a.mp4" FILE "https://example.com/a.pdf" EFFORT HIGH;`, image)
		cmd, err := NewParser(input).Parse(APIMode)
		if err != nil {
			t.Fatal(err)
		}
		if _, err = client.ExecuteUserCommand(cmd); err != nil {
			t.Fatal(err)
		}
		if calls != 1 {
			t.Fatal("request missing")
		}
		server.Close()
	}
}
