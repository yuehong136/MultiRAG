package service

import (
	"encoding/json"
	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"reflect"
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
	for _, raw := range []string{"{}", `{"max_tokens":-1}`, `{"max_tokens":null}`, `{"max_tokens":1.5}`, `{"max_tokens":1,"thinking":"false"}`, "invalid"} {
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

func TestCustomModelMultipleCapabilities(t *testing.T) {
	for _, test := range []struct {
		primary string
		types   []string
		want    []string
	}{
		{"chat", nil, []string{"chat"}},
		{"", []string{"vision", "chat", "asr"}, []string{"image2text", "chat", "speech2text"}},
		{"vision", []string{"image2text", "chat"}, []string{"image2text", "chat"}},
	} {
		got, err := normalizeCustomModelTypes(test.primary, test.types)
		if err != nil || !reflect.DeepEqual(got, test.want) {
			t.Fatalf("%v %v", got, err)
		}
	}
	for _, test := range []struct {
		primary string
		types   []string
	}{
		{"", nil}, {"", []string{}}, {"chat", []string{}}, {"chat", []string{"embedding", "chat"}},
		{"", []string{"vision", "image2text"}}, {"", []string{"qwen"}}, {"", []string{""}},
	} {
		if _, err := normalizeCustomModelTypes(test.primary, test.types); err == nil {
			t.Fatalf("invalid declaration accepted: %v", test)
		}
	}
}

func TestLocalAIInstanceWithoutKey(t *testing.T) {
	provider := &entity.Provider{Name: "LocalAI", URLSuffix: models.URLSuffix{Models: "models"}}
	for _, base := range []string{"http://one.invalid/v1", "http://two.invalid/v1"} {
		raw, err := encodeProviderInstanceExtra("", base)
		if err != nil {
			t.Fatal(err)
		}
		driver, api, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", Extra: raw})
		if err != nil || driver.Name() != "LocalAI" || api.APIKey == nil || *api.APIKey != "" {
			t.Fatalf("%T %v %v", driver, api, err)
		}
		if driver.(*models.LocalAIModel).BaseURL["default"] != base {
			t.Fatal("instance URL lost")
		}
	}
	if _, _, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", Extra: "{}"}); err == nil {
		t.Fatal("missing tenant URL accepted")
	}
}

func TestMinerULocalInstanceWithoutKey(t *testing.T) {
	provider := &entity.Provider{Name: "mineru_local", URLSuffix: models.URLSuffix{DocumentParse: "file_parse", Task: "tasks"}}
	for _, base := range []string{"http://one.invalid", "http://two.invalid"} {
		raw, err := encodeProviderInstanceExtra("", base)
		if err != nil {
			t.Fatal(err)
		}
		driver, api, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", Extra: raw})
		if err != nil || driver.Name() != "mineru_local" || api.APIKey == nil || *api.APIKey != "" || driver.(*models.MinerULocalModel).BaseURL["default"] != base {
			t.Fatalf("%T %v %v", driver, api, err)
		}
		if _, ok := driver.(models.FileParser); !ok {
			t.Fatal("registered local parser does not implement FileParser")
		}
	}
	if _, _, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", Extra: "{}"}); err == nil {
		t.Fatal("missing local endpoint accepted")
	}
}

func TestPaddleOCRLocalInstanceWithoutKey(t *testing.T) {
	provider := &entity.Provider{Name: "PaddleOCR_Local", URLSuffix: models.URLSuffix{OCR: "layout-parsing"}}
	for _, base := range []string{"http://one.invalid", "http://two.invalid"} {
		raw, err := encodeProviderInstanceExtra("office", base)
		if err != nil {
			t.Fatal(err)
		}
		driver, api, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", Extra: raw})
		if err != nil || driver.Name() != "paddleocr_local" || api.APIKey == nil || *api.APIKey != "" || api.Region == nil || *api.Region != "office" {
			t.Fatalf("local OCR instance binding failed: %T %v", driver, err)
		}
		if _, ok := driver.(models.OCRDriver); !ok {
			t.Fatal("local OCR capability missing")
		}
	}
	if _, _, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", Extra: "{}"}); err == nil {
		t.Fatal("missing local endpoint accepted")
	}
	provider.Name = "PaddleOCR"
	raw, err := encodeProviderInstanceExtra("", "http://hosted.invalid")
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", Extra: raw}); err == nil {
		t.Fatal("hosted provider without credentials accepted")
	}
}

func TestCustomModelBoundaryBeforeDatabase(t *testing.T) {
	service := &ModelProviderService{}
	for _, request := range []*AddCustomModelRequest{nil, {}, {ProviderName: "OpenAI", InstanceName: "default", ModelName: "valid", ModelTypes: []string{}, MaxTokens: 0}, {ProviderName: "OpenAI", InstanceName: "default", ModelName: " valid ", ModelTypes: []string{"chat"}, MaxTokens: 0}} {
		if code, err := service.AddCustomModel(request, "owner"); err == nil || code != common.CodeBadRequest {
			t.Fatalf("invalid request reached database: %v %v", code, err)
		}
	}
	for _, raw := range []string{`{"model_name":"valid","model_types":["ocr"]}`, `{"max_tokens":null}`, `{"max_tokens":1.1}`, `{"max_tokens":"1"}`} {
		var request AddCustomModelRequest
		if err := json.Unmarshal([]byte(raw), &request); err == nil {
			t.Fatalf("accepted malformed token limit: %s", raw)
		}
	}
	var request AddCustomModelRequest
	if err := json.Unmarshal([]byte(`{"model_name":"valid","model_types":["ocr"],"max_tokens":0}`), &request); err != nil || request.MaxTokens != 0 {
		t.Fatalf("rejected legal zero: %v", err)
	}
	if extra, err := decodeCustomModelExtra(`{"max_tokens":0,"model_types":["ocr"]}`); err != nil || extra.MaxTokens != 0 {
		t.Fatalf("zero did not survive stored metadata: %v", err)
	}
}
