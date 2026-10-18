package cli

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"
)

func TestModelInferenceParserAndHTTP(t *testing.T) {
	for _, input := range []string{"embed text ' a ' 'b' with 'm@i@p' dimension 16;", "rerank query 'q' document 'a' 'b' with 'm@i@p' top 1;"} {
		t.Run(input, func(t *testing.T) {
			parser := NewParser(input)
			cmd, err := parser.Parse(false)
			if err != nil {
				t.Fatal(err)
			}
			client := skillTestClient(t, func(w http.ResponseWriter, r *http.Request) {
				var body map[string]any
				if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
					t.Error(err)
				}
				if body["model_name"] != "m" || body["instance_name"] != "i" || body["provider_name"] != "p" || r.Header.Get("Authorization") != "Bearer test-credential" || r.Method != "POST" {
					t.Error(body, r.URL)
				}
				if strings.HasPrefix(input, "embed") {
					if r.URL.Path != "/api/v1/embeddings" || body["dimension"] != float64(16) || body["texts"].([]any)[0] != " a " {
						t.Error(body, r.URL)
					}
					fmt.Fprint(w, `{"code":0,"data":[{"index":0,"dimension":16},{"index":1,"dimension":16}]}`)
				} else {
					if r.URL.Path != "/api/v1/rerank" || body["top_n"] != float64(1) || body["query"] != "q" {
						t.Error(body, r.URL)
					}
					fmt.Fprint(w, `{"code":0,"data":[{"index":1,"relevance_score":0}]}`)
				}
			})
			c := &MultiRAGClient{HTTPClient: client, ServerType: "user"}
			response, err := c.ExecuteUserCommand(cmd)
			if err != nil || response == nil {
				t.Fatal(response, err)
			}
		})
	}
	for _, input := range []string{"embed text with 'm@i@p' dimension 16;", "embed text 'a' with 'm@i@p' dimension -1;", "embed text 'a' with 'm@i@p' dimension 1", "embed text 'a' with 'm@i@p' dimension 1 junk;", "rerank query 'q' document with 'm@i@p' top 2;", "rerank query 'q' document 'd' with 'm@i@p' top -1;"} {
		if _, err := NewParser(input).Parse(false); err == nil {
			t.Fatalf("accepted %s", input)
		}
	}
}
func TestModelInferenceBusinessFailures(t *testing.T) {
	for _, body := range []string{`{}`, `{"code":0}`, `{"code":0,"data":null}`, `{"code":1,"data":[]}`, `not json`} {
		client := skillTestClient(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) })
		c := &MultiRAGClient{HTTPClient: client, ServerType: "user"}
		cmd, _ := NewParser("rerank query 'q' document 'd' with 'm@i@p' top 0;").Parse(false)
		if _, err := c.ExecuteUserCommand(cmd); err == nil {
			t.Fatal("accepted", body)
		}
	}
}
