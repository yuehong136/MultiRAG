//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

package common

import (
	"fmt"
	"os"
	"runtime"

	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
)

var (
	atomicLevel = zap.NewAtomicLevelAt(zapcore.InfoLevel)
	// Logger is ready during package initialization. Treat this pointer as read-only;
	// Init and SetLevel update its shared atomic level without replacing the core.
	Logger = newLogger(zapcore.Lock(zapcore.AddSync(os.Stdout)), atomicLevel)
	Sugar  = Logger.Sugar()
)

func newLogger(output zapcore.WriteSyncer, level zap.AtomicLevel) *zap.Logger {
	encoder := zapcore.EncoderConfig{
		TimeKey: "timestamp", LevelKey: "level", NameKey: "logger", MessageKey: "msg",
		StacktraceKey: "stacktrace", LineEnding: zapcore.DefaultLineEnding,
		EncodeLevel:    zapcore.LowercaseLevelEncoder,
		EncodeTime:     zapcore.TimeEncoderOfLayout("2006-01-02 15:04:05"),
		EncodeDuration: zapcore.SecondsDurationEncoder,
	}
	return zap.New(zapcore.NewCore(zapcore.NewConsoleEncoder(encoder), output, level), zap.AddStacktrace(zapcore.ErrorLevel))
}

// Init configures the already available logger. Invalid levels leave it unchanged.
func Init(level string) error { return SetLevel(level) }

// Sync flushes the output. Console descriptors may not support fsync.
func Sync() { _ = Logger.Sync() }

// Fatal records the caller and flushes through zap before exiting with status 1.
func Fatal(msg string, fields ...zap.Field) {
	if _, file, line, ok := runtime.Caller(1); ok {
		fields = append(fields, zap.String("caller", fmt.Sprintf("%s:%d", file, line)))
	}
	Logger.Fatal(msg, fields...)
}

func Info(msg string, fields ...zap.Field)  { Logger.Info(msg, fields...) }
func Error(msg string, err error)           { Logger.Error(msg, zap.Error(err)) }
func Debug(msg string, fields ...zap.Field) { Logger.Debug(msg, fields...) }
func Warn(msg string, fields ...zap.Field)  { Logger.Warn(msg, fields...) }
func IsDebugEnabled() bool                  { return atomicLevel.Enabled(zapcore.DebugLevel) }
func GetLevel() string                      { return atomicLevel.String() }

// SetLevel is safe concurrently with logging and other level updates.
func SetLevel(level string) error {
	var parsed zapcore.Level
	switch level {
	case "debug":
		parsed = zapcore.DebugLevel
	case "info":
		parsed = zapcore.InfoLevel
	case "warn", "warning":
		parsed = zapcore.WarnLevel
	case "error":
		parsed = zapcore.ErrorLevel
	case "fatal":
		parsed = zapcore.FatalLevel
	case "panic":
		parsed = zapcore.PanicLevel
	default:
		return fmt.Errorf("unknown log level: %s", level)
	}
	atomicLevel.SetLevel(parsed)
	return nil
}
