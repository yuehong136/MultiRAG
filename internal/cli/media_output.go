package cli

import (
	"context"
	"encoding/base64"
	"fmt"
	"multirag/internal/entity/models"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

func speechOutputFile(dir, model, format string, audio []byte) (string, error) {
	absDir, err := filepath.Abs(dir)
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(absDir, 0700); err != nil {
		return "", err
	}
	stem := strings.Map(func(r rune) rune {
		if r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-' || r == '_' {
			return r
		}
		return '_'
	}, model)
	if len(stem) > 64 {
		stem = stem[:64]
	}
	if stem == "" {
		stem = "speech"
	}
	file, err := os.CreateTemp(absDir, stem+"_output-*."+format)
	if err != nil {
		return "", err
	}
	path := file.Name()
	_, writeErr := file.Write(audio)
	closeErr := file.Close()
	if writeErr != nil || closeErr != nil {
		os.Remove(path)
		if writeErr != nil {
			return "", writeErr
		}
		return "", closeErr
	}
	return path, nil
}
func speechPlayer(goos, path string) (string, []string, error) {
	if goos == "darwin" {
		if binary, err := exec.LookPath("afplay"); err == nil {
			return binary, []string{path}, nil
		}
	}
	if binary, err := exec.LookPath("mpv"); err == nil {
		return binary, []string{"--no-video", "--really-quiet", "--", path}, nil
	}
	if binary, err := exec.LookPath("ffplay"); err == nil {
		return binary, []string{"-nodisp", "-autoexit", "-loglevel", "error", path}, nil
	}
	return "", nil, fmt.Errorf("PLAY requires afplay on macOS, or mpv/ffplay")
}
func playSpeech(path string) error {
	binary, args, err := speechPlayer(runtime.GOOS, path)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	if err := exec.CommandContext(ctx, binary, args...).Run(); err != nil {
		return fmt.Errorf("play speech: %w", err)
	}
	return nil
}
func finishSpeechOutput(response ResponseIf, cmd *Command, model string, config *models.TTSConfig) (ResponseIf, error) {
	result, ok := response.(*CommonDataResponse)
	if !ok {
		return nil, fmt.Errorf("invalid speech result")
	}
	format, _ := result.Data["format"].(string)
	// Preserve existing callers that do not select an output format.
	if config != nil && (config.Format != "" || len(config.Params) > 0) {
		wanted, err := models.SpeechFormat(config)
		if err != nil {
			return nil, err
		}
		if wanted != format {
			return nil, fmt.Errorf("speech result format differs from request")
		}
	}
	save, _ := cmd.Params["save"].(bool)
	play, _ := cmd.Params["play"].(bool)
	if !save && !play {
		return result, nil
	}
	encoded, _ := result.Data["audio"].(string)
	audio, err := base64.StdEncoding.Strict().DecodeString(encoded)
	if err != nil {
		return nil, fmt.Errorf("invalid speech audio")
	}
	path := ""
	if save {
		dir, _ := cmd.Params["save_path"].(string)
		if strings.TrimSpace(dir) == "" {
			return nil, fmt.Errorf("SAVE requires a directory")
		}
		path, err = speechOutputFile(dir, model, format, audio)
		if err != nil {
			return nil, fmt.Errorf("save speech: %w", err)
		}
		result.Data["saved_path"] = path
	}
	if play {
		if path == "" {
			path, err = speechOutputFile(os.TempDir(), model, format, audio)
			if err != nil {
				return nil, err
			}
			defer os.Remove(path)
		}
		if err := playSpeech(path); err != nil {
			return nil, err
		}
	}
	return result, nil
}
