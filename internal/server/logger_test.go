package server

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"multirag/internal/common"
)

func TestConfigLogsBeforeLoggerInit(t *testing.T) {
	if os.Getenv("MULTIRAG_CONFIG_LOG_CHILD") == "1" {
		if err := FromConfigFile(""); err != nil {
			t.Fatal(err)
		}
		common.Sync()
		return
	}
	executable, err := filepath.Abs(os.Args[0])
	if err != nil {
		t.Fatal(err)
	}
	command := exec.Command(executable, "-test.run=^TestConfigLogsBeforeLoggerInit$")
	command.Dir = t.TempDir()
	command.Env = append(os.Environ(), "MULTIRAG_CONFIG_LOG_CHILD=1")
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("config child failed: %v\n%s", err, output)
	}
	if !bytes.Contains(output, []byte("Config file not found, using environment variables only")) {
		t.Fatalf("startup log missing: %s", output)
	}
}
