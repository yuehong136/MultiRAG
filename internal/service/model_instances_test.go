package service

import (
	"encoding/json"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"testing"
)

func TestProviderInstanceExtraAndDriverIsolation(t *testing.T) {
	for _, raw := range []string{"", "{}", `{"region":"  "}`, `{"region":"default","base_url":""}`} {
		extra, err := decodeProviderInstanceExtra(raw)
		if err != nil || extra.Region != "default" || extra.BaseURL != "" {
			t.Fatalf("%s: %v %v", raw, extra, err)
		}
	}
	for _, raw := range []string{"invalid", `{"base_url":"ftp://invalid"}`, `{"base_url":"https://user:secret@example.test"}`, `{"base_url":"https://example.test?key=secret"}`, `{"region":123}`} {
		if _, err := decodeProviderInstanceExtra(raw); err == nil {
			t.Fatalf("accepted %s", raw)
		}
	}
	original := map[string]string{"default": "http://catalog.invalid/v1"}
	provider := &entity.Provider{Name: "vllm", URL: original, URLSuffix: models.URLSuffix{Chat: "chat/completions"}}
	for _, address := range []string{"http://one.invalid/v1", "http://two.invalid/v1"} {
		raw, err := encodeProviderInstanceExtra("", " "+address+" ")
		if err != nil {
			t.Fatal(err)
		}
		driver, config, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", Extra: raw})
		if err != nil || driver.(*models.VLLMModel).BaseURL["default"] != address || *config.Region != "default" {
			t.Fatalf("%v %v %v", driver, config, err)
		}
	}
	if original["default"] != "http://catalog.invalid/v1" {
		t.Fatal("global URL changed")
	}
	if _, _, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "inactive", Extra: "{}"}); err == nil {
		t.Fatal("disabled instance resolved")
	}
}
func TestCustomModelTypesAndExtra(t *testing.T) {
	for raw, want := range map[string]entity.ModelType{"chat": entity.ModelTypeChat, "vision": entity.ModelTypeImage2Text, "asr": entity.ModelTypeSpeech2Text, "embedding": entity.ModelTypeEmbedding, "rerank": entity.ModelTypeRerank, "tts": entity.ModelTypeTTS, "ocr": entity.ModelTypeOCR} {
		typ, err := normalizeCustomModelType(raw)
		if err != nil || typ != want {
			t.Fatalf("%s: %v %v", raw, typ, err)
		}
	}
	if _, err := normalizeCustomModelType("qwen"); err == nil {
		t.Fatal("family accepted as capability")
	}
	for _, raw := range []string{"{}", `{"max_tokens":0}`, `{"max_tokens":1.5}`, `{"max_tokens":1,"thinking":"false"}`, "invalid"} {
		if _, err := decodeCustomModelExtra(raw); err == nil {
			t.Fatalf("accepted %s", raw)
		}
	}
	extra, err := decodeCustomModelExtra(`{"max_tokens":2048,"thinking":false}`)
	if err != nil || extra.Thinking == nil || *extra.Thinking {
		t.Fatalf("%v %v", extra, err)
	}
	raw, err := json.Marshal(extra)
	if err != nil || string(raw) != `{"max_tokens":2048,"thinking":false}` {
		t.Fatalf("%s %v", raw, err)
	}
}
