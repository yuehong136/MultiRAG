package models

import (
	"context"
	"encoding/base64"
	"testing"
)

func TestInputAudioContentValidation(t *testing.T) {
	for _, format := range []string{"wav", "mp3"} {
		if _, err := ContentParts([]map[string]any{{"type": "input_audio", "input_audio": map[string]any{"format": format, "data": base64.StdEncoding.EncodeToString([]byte("controlled"))}}}); err != nil {
			t.Fatal(err)
		}
	}
	for _, audio := range []any{nil, "wrong", map[string]any{}, map[string]any{"format": "ogg", "data": "YQ=="}, map[string]any{"format": "wav", "data": "%%%"}, map[string]any{"format": "wav", "data": ""}} {
		if _, err := ContentParts([]map[string]any{{"type": "input_audio", "input_audio": audio}}); err == nil {
			t.Error("invalid audio accepted", audio)
		}
	}
}
func TestBuiltInMediaCapabilitiesAreExplicit(t *testing.T) {
	for _, provider := range []string{"cohere", "fishaudio", "stepfun", "baichuan", "upstage", "lm-studio", "xai", "ollama", "vllm", "volcengine", "openrouter", "nvidia", "huggingface", "baidu", "openai", "openai-api-compatible", "google", "zhipu-ai", "deepseek", "moonshot", "minimax", "gitee", "siliconflow", "aliyun", "unsupported-provider"} {
		driver, err := NewModelFactory().CreateModelDriver(provider, map[string]string{"default": "http://127.0.0.1:1"}, URLSuffix{})
		if err != nil {
			t.Fatal(err)
		}
		if _, ok := driver.(ASRDriver); ok {
			t.Error("unexpected ASR capability", provider)
		}
		if _, ok := driver.(TTSDriver); ok {
			t.Error("unexpected TTS capability", provider)
		}
		if _, ok := driver.(ASRStreamDriver); ok {
			t.Error("unexpected streaming ASR capability", provider)
		}
		if _, ok := driver.(TTSStreamDriver); ok {
			t.Error("unexpected streaming TTS capability", provider)
		}
		if _, ok := driver.(OCRDriver); ok && provider != "gitee" {
			t.Error("unexpected OCR capability", provider)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := requestContext(&APIConfig{Context: ctx}).Err(); err == nil {
		t.Fatal("cancel lost")
	}
}

func TestMediaTextPresence(t *testing.T) {
	empty := ""
	if err := ValidateASRResponse(&ASRResponse{Text: &empty}); err != nil {
		t.Fatal("silent transcription rejected", err)
	}
	if err := ValidateOCRResponse(&OCRResponse{Text: &empty}); err != nil {
		t.Fatal("blank page rejected", err)
	}
	if ValidateASRResponse(&ASRResponse{}) == nil || ValidateOCRResponse(&OCRResponse{}) == nil {
		t.Fatal("missing text treated as explicit empty")
	}
}
