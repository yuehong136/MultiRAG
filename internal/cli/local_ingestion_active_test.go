package cli

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"github.com/google/uuid"
	"multirag/internal/testsupport"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestLocalIngestionCLIExecutesActualContent(t *testing.T) {
	journal := testsupport.NewCase(t)
	root := filepath.Join(os.TempDir(), "after22-cli-"+uuid.NewString())
	journal.Event("reserve-local-fixtures", map[string]any{"path": root})
	if err := os.Mkdir(root, 0700); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		err := os.RemoveAll(root)
		_, readErr := os.Stat(root)
		journal.Event("local-fixtures-cleanup", map[string]any{"path": root, "remove_ok": err == nil, "absent": os.IsNotExist(readErr)})
		if err != nil || !os.IsNotExist(readErr) {
			t.Error("fixture cleanup")
		}
	})
	write := func(name string, data []byte) string {
		t.Helper()
		p := filepath.Join(root, name)
		if err := os.WriteFile(p, data, 0600); err != nil {
			t.Fatal(err)
		}
		return p
	}
	text := write("source.txt", []byte("one\n\n二"))
	dsl := write("pipeline.json", []byte(`{"pipeline":[{"operator":"split","strategy":"paragraph"}]}`))
	bad := write("bad.json", []byte(`{"pipeline":[{"operator":"split","typo":true}]}`))
	cfg := write("config.json", []byte(`{"lib_type":"builtin"}`))
	var buffer bytes.Buffer
	zw := zip.NewWriter(&buffer)
	part, err := zw.Create("word/document.xml")
	if err != nil {
		t.Fatal(err)
	}
	part.Write([]byte(`<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Actual Office text</w:t></w:r></w:p></w:body></w:document>`))
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	doc := write("source.docx", buffer.Bytes())
	commands := []string{"CHUNK '" + text + "' WITH '" + dsl + "';", "EXPLAIN CHUNK 'missing.txt' WITH '" + dsl + "';", "PARSE FILE '" + doc + "' WITH '" + cfg + "';"}
	client := NewMultiRAGClient("user")
	client.HTTPClient.client.Transport = &registryTransport{}
	for i, input := range commands {
		if looksLikeContextEngine(input) {
			t.Fatal("local command escaped SQL parser")
		}
		cmd, err := NewParser(input).Parse(false)
		if err != nil {
			t.Fatal(input, err)
		}
		result, err := client.ExecuteCommand(cmd)
		if err != nil {
			t.Fatal(input, err)
		}
		switch i {
		case 0:
			rows := result.(*CommonResponse).Data
			if len(rows) != 2 || rows[1]["content"] != "二" || rows[1]["size"] != 1 {
				t.Fatal(rows)
			}
		case 1:
			if result.(*CommonDataResponse).Data["explanation"] == "" {
				t.Fatal("empty explanation")
			}
		case 2:
			if !strings.Contains(result.(*CommonDataResponse).Data["text"].(string), "Actual Office text") {
				t.Fatal(result)
			}
		}
		binary := os.Getenv("MULTIRAG_M_CLI")
		if binary != "" {
			journal.Event("reserve-cli-subprocess", map[string]any{"binary": binary, "command": input})
			process := exec.Command(binary, input, "-o", "json")
			out, err := process.CombinedOutput()
			journal.Event("cli-subprocess-exit", map[string]any{"pid": process.Process.Pid, "success": err == nil})
			if err != nil || !json.Valid(bytes.TrimSpace(out)) {
				t.Fatalf("actual CLI invalid output: %s %v", out, err)
			}
		}
	}
	for _, input := range []string{"CHUNK '" + text + "' WITH '" + bad + "';", "PARSE FILE 'missing.pdf' WITH '" + cfg + "';"} {
		cmd, err := NewParser(input).Parse(false)
		if err != nil {
			t.Fatal(err)
		}
		if out, err := client.ExecuteCommand(cmd); err == nil || out != nil {
			t.Fatal("invalid local input succeeded", input, out, err)
		}
	}
}
