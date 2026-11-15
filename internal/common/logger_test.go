package common

import (
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	"go.uber.org/zap/zaptest/observer"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

func TestLoggerCanonicalLevelsAndCapturedLogger(t *testing.T) {
	// Queries are valid before Init and keep the same atomic controller when
	// the logger is reinitialized by a server after loading configuration.
	_ = IsDebugEnabled()
	_ = GetLevel()
	if err := Init("info", ""); err != nil {
		t.Fatal(err)
	}
	core, logs := observer.New(atomicLevel)
	captured := Logger.WithOptions(zap.WrapCore(func(zapcore.Core) zapcore.Core { return core }))
	if err := SetLevel("error"); err != nil {
		t.Fatal(err)
	}
	captured.Info("hidden")
	if err := SetLevel("debug"); err != nil {
		t.Fatal(err)
	}
	captured.Debug("visible")
	if logs.Len() != 1 || logs.All()[0].Message != "visible" {
		t.Fatal(logs.All())
	}
	if err := Init("warn", ""); err != nil {
		t.Fatal(err)
	}
	captured.Info("hidden after reinitialization")
	captured.Warn("visible after reinitialization")
	if logs.Len() != 2 {
		t.Fatal("reinitialization detached captured log levels", logs.All())
	}
	if err := SetLevel("debug"); err != nil {
		t.Fatal(err)
	}
	if err := SetLevel("invalid"); err == nil || GetLevel() != "debug" {
		t.Fatal("invalid level changed state")
	}
	var group sync.WaitGroup
	for range 8 {
		group.Go(func() {
			for range 100 {
				SetLevel("info")
				GetLevel()
				IsDebugEnabled()
				SetLevel("debug")
			}
		})
	}
	group.Wait()
	Sync()
}

func TestLoggerCreatesColdStartDirectory(t *testing.T) {
	t.Chdir(t.TempDir())
	if err := Init("info", "cold.log"); err != nil {
		t.Fatal(err)
	}
	Info("cold startup")
	Sync()
	data, err := os.ReadFile(filepath.Join("logs", "cold.log"))
	if err != nil || len(data) == 0 {
		t.Fatal("cold startup log missing", err)
	}
}
