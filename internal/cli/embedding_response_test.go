package cli

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
)

func TestEmbeddingCLIRejectsMalformedVectors(t *testing.T) {
	for _, body := range []string{
		`{}`, `{"code":0}`, `{"code":0,"data":null}`, `{"code":1,"data":[]}`,
		`{"code":0,"data":[{"index":0,"dimension":2}]}`,
		`{"code":0,"data":[{"embedding":[1,2]}]}`,
		`{"code":0,"data":[{"index":0,"embedding":[null,1]}]}`,
		`{"code":0,"data":[{"index":0,"embedding":[1]}]}`,
		`{"code":0,"data":[{"index":1,"embedding":[1,2]}]}`,
	} {
		client := skillTestClient(t, func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, body) })
		c := &MultiRAGClient{HTTPClient: client, ServerType: "user"}
		cmd, _ := NewParser("embed text 'a' with 'm@i@p' dimension 2;").Parse(false)
		if _, err := c.ExecuteUserCommand(cmd); err == nil {
			t.Fatal("accepted malformed embedding", body)
		}
	}
}

func TestEmbeddingCLIReordersAndDisplaysComputedDimension(t *testing.T) {
	client := skillTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"code":0,"data":[{"index":1,"embedding":[1,0]},{"index":0,"embedding":[0,2]}]}`)
	})
	c := &MultiRAGClient{HTTPClient: client, ServerType: "user"}
	cmd, _ := NewParser("embed text 'a' 'b' with 'm@i@p' dimension 2;").Parse(false)
	result, err := c.ExecuteUserCommand(cmd)
	if err != nil {
		t.Fatal(err)
	}
	response, ok := result.(*EmbeddingsResponse)
	if !ok || response.Data[0].Index != 0 || response.Data[0].Embedding[1] != 2 || response.Data[1].Index != 1 {
		t.Fatal("input identity lost", response)
	}
	response.SetOutputFormat(OutputFormatJSON)
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	old := os.Stdout
	defer func() { os.Stdout = old }()
	os.Stdout = writer
	response.PrintOut()
	writer.Close()
	os.Stdout = old
	raw, err := io.ReadAll(reader)
	reader.Close()
	if err != nil {
		t.Fatal(err)
	}
	output := string(raw)
	if !strings.Contains(output, "dimension") || strings.Contains(output, "embedding") {
		t.Fatal("CLI dimension summary changed", output)
	}
}
