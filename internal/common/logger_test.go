package common

import (
	"bytes"
	"errors"
	"os"
	"os/exec"
	"strings"
	"sync"
	"testing"

	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	"go.uber.org/zap/zaptest/observer"
)

func TestLoggerBeforeInit(t *testing.T) {
	if os.Getenv("MULTIRAG_LOGGER_CHILD") == "startup" {
		if GetLevel() != "info" || IsDebugEnabled() {
			t.Fatal("unexpected startup level")
		}
		Info("startup-info")
		Warn("startup-warn")
		Error("startup-error", errors.New("expected"))
		Sugar.Infow("startup-sugar", "key", "value")
		Sync()
		return
	}
	command := exec.Command(os.Args[0], "-test.run=^TestLoggerBeforeInit$")
	command.Env = append(os.Environ(), "MULTIRAG_LOGGER_CHILD=startup")
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("child failed: %v\n%s", err, output)
	}
	for _, message := range []string{"startup-info", "startup-warn", "startup-error", "startup-sugar"} {
		if !bytes.Contains(output, []byte(message)) {
			t.Errorf("startup log lost: %s\n%s", message, output)
		}
	}
}

func TestLogLevelKeepsCapturedLoggers(t *testing.T) {
	defer SetLevel(GetLevel())
	original, sugar := Logger, Sugar
	core, observed := observer.New(atomicLevel)
	captured := Logger.WithOptions(zap.WrapCore(func(zapcore.Core) zapcore.Core { return core })).With(zap.String("component", "captured"))
	if err := Init("error"); err != nil {
		t.Fatal(err)
	}
	captured.Info("hidden")
	if err := Init("debug"); err != nil {
		t.Fatal(err)
	}
	captured.Debug("visible")
	if Logger != original || Sugar != sugar || !IsDebugEnabled() {
		t.Fatal("logger identity or level changed incorrectly")
	}
	if observed.Len() != 1 || observed.All()[0].Message != "visible" {
		t.Fatal(observed.All())
	}
	for _, level := range []string{"info", "warn", "warning", "error", "panic", "fatal", "debug"} {
		if err := SetLevel(level); err != nil {
			t.Fatal(err)
		}
		want := level
		if want == "warning" {
			want = "warn"
		}
		if GetLevel() != want {
			t.Fatalf("%s: %s", level, GetLevel())
		}
	}
	before := GetLevel()
	if Init("invalid") == nil || GetLevel() != before {
		t.Fatal("invalid Init modified level")
	}
	if SetLevel("") == nil || GetLevel() != before {
		t.Fatal("invalid SetLevel modified level")
	}
}

func TestLogLevelConcurrent(t *testing.T) {
	defer SetLevel(GetLevel())
	var group sync.WaitGroup
	for i := 0; i < 8; i++ {
		group.Add(1)
		go func() {
			defer group.Done()
			for n := 0; n < 100; n++ {
				_ = Init("error")
				_ = SetLevel("info")
				_ = GetLevel()
				_ = IsDebugEnabled()
				Debug("suppressed concurrent message")
			}
		}()
	}
	group.Wait()
}

type syncRecorder struct {
	bytes.Buffer
	syncs int
}

func (s *syncRecorder) Sync() error { s.syncs++; return nil }

func TestLogSync(t *testing.T) {
	previous := Logger
	defer func() { Logger = previous }()
	sink := &syncRecorder{}
	Logger = newLogger(sink, zap.NewAtomicLevelAt(zapcore.InfoLevel))
	Info("before-sync")
	Sync()
	if sink.syncs != 1 || !strings.Contains(sink.String(), "before-sync") {
		t.Fatal("output was not flushed")
	}
}

type fatalSink struct{}

func (fatalSink) Write(data []byte) (int, error) { return os.Stdout.Write(data) }
func (fatalSink) Sync() error                    { _, err := os.Stderr.WriteString("fatal-sync-confirmed\n"); return err }

func TestLogFatalFlushAndCaller(t *testing.T) {
	if os.Getenv("MULTIRAG_LOGGER_CHILD") == "fatal" {
		Logger = newLogger(fatalSink{}, zap.NewAtomicLevelAt(zapcore.InfoLevel))
		Fatal("fatal-visible")
		return
	}
	command := exec.Command(os.Args[0], "-test.run=^TestLogFatalFlushAndCaller$")
	command.Env = append(os.Environ(), "MULTIRAG_LOGGER_CHILD=fatal")
	output, err := command.CombinedOutput()
	var exit *exec.ExitError
	if !errors.As(err, &exit) || exit.ExitCode() != 1 {
		t.Fatalf("expected exit 1, got %v\n%s", err, output)
	}
	for _, value := range []string{"fatal-visible", "logger_test.go:", "fatal-sync-confirmed"} {
		if !bytes.Contains(output, []byte(value)) {
			t.Fatalf("missing %s: %s", value, output)
		}
	}
}
