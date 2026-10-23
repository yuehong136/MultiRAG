package models

import (
	"encoding/base64"
	"errors"
	"fmt"
	"strings"
)

const MaxMediaBytes = 20 * 1024 * 1024

var ErrUnsupportedCapability = errors.New("model driver does not support this operation")

// DriverFactory constructs a request-scoped driver from trusted instance settings.
// The default factory implements it; alternate adapters can add explicit capabilities.
type DriverFactory interface {
	CreateModelDriver(string, map[string]string, URLSuffix) (ModelDriver, error)
}

// Media capabilities are optional. A catalog model type does not imply a working driver.
// Streaming adapters invoke sender synchronously and return any sender error.
type ASRDriver interface {
	TranscribeAudio(*string, []byte, *APIConfig, *ASRConfig) (*ASRResponse, error)
}
type ASRStreamDriver interface {
	TranscribeAudioWithSender(*string, []byte, *APIConfig, *ASRConfig, func(*ASRResponse) error) error
}
type TTSDriver interface {
	AudioSpeech(*string, string, *APIConfig, *TTSConfig) (*TTSResponse, error)
}
type TTSStreamDriver interface {
	AudioSpeechWithSender(*string, string, *APIConfig, *TTSConfig, func(*TTSResponse) error) error
}
type OCRDriver interface {
	OCRFile(*string, []byte, *APIConfig, *OCRConfig) (*OCRResponse, error)
}

type ASRResponse struct {
	Text string `json:"text"`
}
type OCRResponse struct {
	Text string `json:"text"`
}
type TTSResponse struct {
	Audio  []byte `json:"audio"`
	Format string `json:"format"`
}
type ASRConfig struct {
	Language []string `json:"language,omitempty"`
	Prompt   string   `json:"prompt,omitempty"`
	Format   string   `json:"format,omitempty"`
}
type TTSConfig struct {
	Voice  string `json:"voice,omitempty"`
	Format string `json:"format,omitempty"`
}
type OCRConfig struct {
	Language []string `json:"language,omitempty"`
}

func ValidateMediaBytes(data []byte) error {
	if len(data) == 0 || len(data) > MaxMediaBytes {
		return fmt.Errorf("media must contain 1 to %d bytes", MaxMediaBytes)
	}
	return nil
}
func DecodeAudioInput(encoded, format string) ([]byte, error) {
	if format != "wav" && format != "mp3" {
		return nil, fmt.Errorf("audio format must be wav or mp3")
	}
	return DecodeMediaData(encoded)
}

// DecodeMediaData accepts bounded standard base64; JSON arrays are not file input.
func DecodeMediaData(encoded string) ([]byte, error) {
	if len(encoded) > base64.StdEncoding.EncodedLen(MaxMediaBytes) {
		return nil, fmt.Errorf("media exceeds 20 MiB")
	}
	data, err := base64.StdEncoding.Strict().DecodeString(encoded)
	if err != nil {
		return nil, fmt.Errorf("media requires valid base64")
	}
	if err := ValidateMediaBytes(data); err != nil {
		return nil, err
	}
	return data, nil
}
func ValidateASRResponse(r *ASRResponse) error {
	if r == nil || r.Text == "" || len(r.Text) > MaxMediaBytes {
		return fmt.Errorf("empty or oversized transcription result")
	}
	return nil
}
func ValidateOCRResponse(r *OCRResponse) error {
	if r == nil || r.Text == "" || len(r.Text) > MaxMediaBytes {
		return fmt.Errorf("empty or oversized OCR result")
	}
	return nil
}
func ValidateTTSResponse(r *TTSResponse) error {
	if r == nil {
		return fmt.Errorf("empty speech result")
	}
	if err := ValidateMediaBytes(r.Audio); err != nil {
		return err
	}
	if !ValidAudioFormat(r.Format) {
		return fmt.Errorf("invalid speech audio format")
	}
	return nil
}
func ValidAudioFormat(format string) bool {
	switch format {
	case "wav", "mp3", "pcm", "flac", "opus", "aac":
		return true
	}
	return false
}
func ValidateSpeechText(text string) error {
	if strings.TrimSpace(text) == "" || len(text) > MaxMediaBytes {
		return fmt.Errorf("speech text must be nonempty and at most 20 MiB")
	}
	return nil
}

func ValidateASRConfig(config *ASRConfig) error {
	if config != nil && config.Format != "" && config.Format != "wav" && config.Format != "mp3" {
		return fmt.Errorf("ASR format must be wav or mp3")
	}
	return nil
}
func ValidateTTSConfig(config *TTSConfig) error {
	if config != nil && config.Format != "" && !ValidAudioFormat(config.Format) {
		return fmt.Errorf("invalid speech format")
	}
	return nil
}
