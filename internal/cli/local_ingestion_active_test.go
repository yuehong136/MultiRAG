package cli

import (
	"fmt"
	"github.com/google/uuid"
	"io"
	"multirag/internal/testsupport"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestLocalIngestionCLIExecutesActualContent(t *testing.T) {
	journal := testsupport.NewCase(t)
	root := filepath.Join(os.TempDir(), "go-align-local-cli-"+uuid.NewString())
	journal.Event("reserve-local-fixtures", map[string]any{"path": root})
	if err := os.Mkdir(root, 0700); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		err := os.RemoveAll(root)
		_, readErr := os.Stat(root)
		journal.Event("local-fixtures-cleanup", map[string]any{"path": root, "absent": os.IsNotExist(readErr)})
		if err != nil || !os.IsNotExist(readErr) {
			t.Error("fixture cleanup")
		}
	})
	write := func(name, data string) string {
		path := filepath.Join(root, name)
		if err := os.WriteFile(path, []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
		return path
	}
	source := write("source.txt", "one\n\n二")
	dsl := write("pipeline.json", `{"pipeline":[{"operator":"split","strategy":"paragraph"}]}`)
	repeated := write("repeated.json", `{"pipeline":[{"operator":"preprocess"},{"operator":"preprocess"},{"operator":"split","strategy":"paragraph"},{"operator":"split","strategy":"char"}]}`)
	bad := write("bad.json", "{")
	commands := []struct{ input, want string }{
		{fmt.Sprintf("CHUNK '%s' WITH '%s';", source, dsl), "二"},
		{fmt.Sprintf("EXPLAIN CHUNK 'missing.txt' WITH '%s';", dsl), "Chunk Pipeline Plan:"},
		{fmt.Sprintf("CHUNK '%s' WITH '%s';", source, repeated), "二"},
	}
	fixtures := os.Getenv("MULTIRAG_OFFICE_FIXTURES")
	if fixtures == "" {
		t.Fatal("registered native fixtures required")
	}
	for _, ext := range []string{"doc", "docx", "ppt", "pptx", "xls", "xlsx"} {
		commands = append(commands, struct{ input, want string }{fmt.Sprintf("PARSE FILE '%s';", filepath.Join(fixtures, "sample."+ext)), "Document context:"})
	}
	client := &CLI{Config: &CommandLineConfig{CLIMode: APIMode}}
	for _, item := range commands {
		cmd, err := NewParser(item.input).Parse(APIMode)
		if err != nil {
			t.Fatal(item.input, err)
		}
		journal.Event("reserve-stdout-pipe", map[string]any{"command": item.input})
		reader, writer, err := os.Pipe()
		if err != nil {
			t.Fatal(err)
		}
		output := make(chan []byte, 1)
		go func() { raw, _ := io.ReadAll(reader); reader.Close(); output <- raw }()
		previous := os.Stdout
		os.Stdout = writer
		result, executeErr := client.ExecuteCommand(cmd)
		os.Stdout = previous
		writer.Close()
		raw := string(<-output)
		if executeErr != nil || result == nil {
			t.Fatal(item.input, executeErr)
		}
		if response, ok := result.(*ExplainResponse); ok {
			raw += response.Message
		}
		if !strings.Contains(raw, item.want) {
			t.Fatalf("canonical output missing %q: %s", item.want, raw)
		}
		if binary := os.Getenv("MULTIRAG_M_CLI"); binary != "" {
			journal.Event("reserve-cli-subprocess", map[string]any{"binary": binary, "command": item.input, "cwd": root})
			process := exec.Command(binary, item.input)
			process.Dir = root
			out, err := process.CombinedOutput()
			journal.Event("cli-subprocess-exit", map[string]any{"pid": process.Process.Pid, "success": err == nil})
			if err != nil || !strings.Contains(string(out), item.want) {
				t.Fatalf("real CLI %v: %s", err, out)
			}
		}
	}
	for _, input := range []string{fmt.Sprintf("CHUNK '%s' WITH '%s';", source, bad), "PARSE FILE 'missing.pdf';"} {
		cmd, err := NewParser(input).Parse(APIMode)
		if err != nil {
			t.Fatal(err)
		}
		if out, err := client.ExecuteCommand(cmd); err == nil || out != nil {
			t.Fatal("invalid local input succeeded", input, out, err)
		}
	}
}
