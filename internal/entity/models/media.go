package models

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"strings"
	"unicode"
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

// OCRURLDriver forwards an image URL to the provider without fetching it locally.
type OCRURLDriver interface {
	OCRURL(*string, string, *APIConfig, *OCRConfig) (*OCRResponse, error)
}

func ValidateOCRURL(value string) error {
	parsed, err := url.Parse(value)
	if err != nil || len(value) > 8192 || strings.TrimSpace(value) != value || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" || parsed.User != nil || parsed.Fragment != "" {
		return fmt.Errorf("OCR URL must be an HTTP(S) image URL without credentials or fragment")
	}
	return nil
}

type ASRResponse struct {
	Text *string `json:"text"`
}
type OCRResponse struct {
	Text *string `json:"text"`
}
type TTSResponse struct {
	Audio  []byte `json:"audio"`
	Format string `json:"format"`
}
type ASRConfig struct {
	Params   map[string]any `json:"params,omitempty"`
	Language []string       `json:"language,omitempty"`
	Prompt   string         `json:"prompt,omitempty"`
	Format   string         `json:"format,omitempty"`
}
type TTSConfig struct {
	Params map[string]any `json:"params,omitempty"`
	Voice  string         `json:"voice,omitempty"`
	Format string         `json:"format,omitempty"`
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
	if r == nil || r.Text == nil || len(*r.Text) > MaxMediaBytes {
		return fmt.Errorf("missing or oversized transcription result")
	}
	return nil
}
func ValidateOCRResponse(r *OCRResponse) error {
	if r == nil || r.Text == nil || len(*r.Text) > MaxMediaBytes {
		return fmt.Errorf("missing or oversized OCR result")
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
	if config != nil {
		if _, err := CloneMediaParams(config.Params); err != nil {
			return err
		}
	}
	if config != nil && config.Format != "" && config.Format != "wav" && config.Format != "mp3" {
		return fmt.Errorf("ASR format must be wav or mp3")
	}
	return nil
}
func ValidateTTSConfig(config *TTSConfig) error {
	if config != nil {
		if _, err := CloneMediaParams(config.Params); err != nil {
			return err
		}
	}
	_, err := SpeechFormat(config)
	return err
}

const MaxMediaParamsBytes = 64 * 1024

// CloneMediaParams bounds dynamic options and isolates nested objects from adapters.
// Identity, credentials, input and streaming are selected by the trusted call chain.
func CloneMediaParams(params map[string]any) (map[string]any, error) {
	for key := range params {
		if key == "" || len(key) > 128 || strings.IndexFunc(key, unicode.IsControl) >= 0 || strings.ContainsAny(key, `"\`) {
			return nil, fmt.Errorf("invalid media param name")
		}
		switch strings.ToLower(key) {
		case "model", "text", "input", "stream", "file", "audio", "api_key", "authorization", "base_url", "url", "region":
			return nil, fmt.Errorf("media params cannot override %s", key)
		}
	}
	raw, err := json.Marshal(params)
	if err != nil || len(raw) > MaxMediaParamsBytes {
		return nil, fmt.Errorf("media params must be JSON and at most 64 KiB")
	}
	result := map[string]any{}
	if params != nil {
		if err := json.Unmarshal(raw, &result); err != nil {
			return nil, fmt.Errorf("invalid media params")
		}
	}
	return result, nil
}

// SpeechFormat uses the explicit format first, then known provider parameter paths.
func SpeechFormat(config *TTSConfig) (string, error) {
	format := ""
	if config != nil {
		values := []any{}
		for _, key := range []string{"format", "response_format"} {
			if v, ok := config.Params[key]; ok {
				values = append(values, v)
			}
		}
		if v, ok := config.Params["audio_setting"]; ok {
			settings, ok := v.(map[string]any)
			if !ok {
				return "", fmt.Errorf("audio_setting must be an object")
			}
			if v, ok := settings["format"]; ok {
				values = append(values, v)
			}
		}
		for _, v := range values {
			next, ok := v.(string)
			if !ok || !ValidAudioFormat(next) {
				return "", fmt.Errorf("invalid speech format in params")
			}
			if config.Format == "" && format != "" && format != next {
				return "", fmt.Errorf("conflicting speech formats in params")
			}
			format = next
		}
		if config.Format != "" {
			format = config.Format
		}
	}
	if format == "" {
		format = "mp3"
	}
	if !ValidAudioFormat(format) {
		return "", fmt.Errorf("invalid speech format")
	}
	return format, nil
}
