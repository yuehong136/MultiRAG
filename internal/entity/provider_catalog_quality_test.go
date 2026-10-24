package entity

import (
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"

	"multirag/internal/entity/models"
)

type catalogModelContract struct {
	name       string
	modelTypes []string
	thinking   *ModelThinking
}

func newCatalogManager(t *testing.T) *ProviderManager {
	t.Helper()
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	return manager
}

func requireCatalogProvider(t *testing.T, manager *ProviderManager, name string, driver models.ModelDriver, suffix models.URLSuffix) {
	t.Helper()
	provider := manager.FindProvider(strings.ToLower(name))
	if provider == nil || provider.ModelDriver == nil {
		t.Fatalf("%s catalog or factory missing: %#v", name, provider)
	}
	if got := reflect.TypeOf(provider.ModelDriver); got != reflect.TypeOf(driver) {
		t.Fatalf("%s driver = %v, want %T", name, got, driver)
	}
	if got := provider.ModelDriver.Name(); got != strings.ToLower(name) {
		t.Errorf("%s driver name = %q", name, got)
	}
	if provider.URLSuffix != suffix {
		t.Errorf("%s URL suffixes = %#v, want %#v", name, provider.URLSuffix, suffix)
	}
}

// These are named compatibility anchors, not a snapshot of every model offered
// by a supplier. New entries are covered by the metadata quality checks below.
func requireCatalogModels(t *testing.T, manager *ProviderManager, provider string, contracts []catalogModelContract) {
	t.Helper()
	listed, err := manager.ListModels(provider)
	if err != nil {
		t.Fatal(err)
	}
	for _, contract := range contracts {
		t.Run(contract.name, func(t *testing.T) {
			model, err := manager.GetModelByName(strings.ToLower(provider), contract.name)
			if err != nil || model == nil {
				t.Fatalf("required model missing: %v", err)
			}
			wantTypes := slices.Clone(contract.modelTypes)
			gotTypes := slices.Clone(model.ModelTypes)
			slices.Sort(wantTypes)
			slices.Sort(gotTypes)
			wantMap := make(map[string]bool, len(wantTypes))
			for _, modelType := range wantTypes {
				wantMap[modelType] = true
			}
			if !slices.Equal(gotTypes, wantTypes) || !reflect.DeepEqual(model.ModelTypeMap, wantMap) {
				t.Errorf("capabilities = %v / %v, want exactly %v", model.ModelTypes, model.ModelTypeMap, wantTypes)
			}
			if !reflect.DeepEqual(model.Thinking, contract.thinking) {
				t.Errorf("thinking = %#v, want %#v", model.Thinking, contract.thinking)
			}
			// ListModels is consumed by the provider HTTP/CLI listing. Keep both
			// capability types and the optional thinking feature in that response.
			found := false
			for _, row := range listed {
				if row["name"] != contract.name {
					continue
				}
				found = true
				listedTypes, ok := row["model_types"].([]string)
				listedTypes = slices.Clone(listedTypes)
				slices.Sort(listedTypes)
				if !ok || !slices.Equal(listedTypes, wantTypes) {
					t.Errorf("listed capabilities = %#v, want exactly %v", row["model_types"], wantTypes)
				}
				features, ok := row["features"].([]string)
				if !ok || slices.Contains(features, "thinking") != (contract.thinking != nil) {
					t.Errorf("listed features = %#v", row["features"])
				}
			}
			if !found {
				t.Error("required model missing from ListModels")
			}
			for _, modelType := range []string{"chat", "vision", "embedding", "rerank", "tts", "asr", "speech2text", "ocr"} {
				response := manager.SearchModelInfo(provider, contract.name, "type", modelType)
				if wantMap[modelType] {
					if response.Code != 0 || len(response.Data) != 1 {
						t.Errorf("type %s filter lost model: %#v", modelType, response)
					}
				} else if response.Code != 400 || len(response.Data) != 0 {
					t.Errorf("type %s filter advertised unsupported capability: %#v", modelType, response)
				}
			}
			if !wantMap["chat"] {
				url, resultURL, err := manager.GetModelUrl(provider, contract.name, "chat")
				if err == nil || url != nil || resultURL != nil {
					t.Errorf("non-chat model resolved a chat URL: %v, %v, %v", url, resultURL, err)
				}
			}
		})
	}
}

func TestProviderCatalogIdentifiersAndCapabilities(t *testing.T) {
	manager := newCatalogManager(t)
	for _, name := range []string{"Baidu", "CoHere", "FishAudio", "StepFun", "Baichuan", "Upstage", "OpenRouter", "VolcEngine"} {
		t.Run(name, func(t *testing.T) {
			provider := manager.FindProvider(name)
			if provider == nil {
				t.Fatal("required provider missing")
			}
			providerCount := 0
			for _, candidate := range manager.Providers {
				if strings.EqualFold(candidate.Name, name) {
					providerCount++
				}
			}
			if providerCount != 1 {
				t.Errorf("provider identity resolves to %d entries, want one", providerCount)
			}
			seenNames := make([]string, 0, len(provider.Models))
			for _, model := range provider.Models {
				if model == nil {
					t.Fatal("null model entry")
				}
				if model.Name == "" || strings.TrimSpace(model.Name) != model.Name || strings.Contains(model.Name, "@") {
					t.Errorf("invalid model identifier %q", model.Name)
				}
				if slices.ContainsFunc(seenNames, func(candidate string) bool { return strings.EqualFold(candidate, model.Name) }) {
					t.Errorf("duplicate case-insensitive model identifier %q", model.Name)
				}
				seenNames = append(seenNames, model.Name)
				seenTypes := make(map[string]bool)
				for _, modelType := range model.ModelTypes {
					// Canonical service types plus the vision/asr catalog aliases
					// normalized by normalizeCustomModelType at instance binding.
					switch modelType {
					case "chat", "vision", "image2text", "embedding", "rerank", "tts", "asr", "speech2text", "ocr", "doc_parse", "async_chat":
					default:
						t.Errorf("%s has unsupported capability %q", model.Name, modelType)
					}
					if seenTypes[modelType] {
						t.Errorf("%s has duplicate capability %q", model.Name, modelType)
					}
					seenTypes[modelType] = true
				}
				if !reflect.DeepEqual(model.ModelTypeMap, seenTypes) {
					t.Errorf("%s capability lookup differs from declared types", model.Name)
				}
				if model.Thinking != nil && !seenTypes["chat"] && !seenTypes["vision"] && !seenTypes["image2text"] {
					t.Errorf("%s non-chat capability inherited thinking", model.Name)
				}
				if seenTypes["tts"] {
					if _, ok := provider.ModelDriver.(models.TTSDriver); !ok {
						t.Errorf("%s advertises TTS without a TTS driver", model.Name)
					}
				}
				if seenTypes["asr"] || seenTypes["speech2text"] {
					if _, ok := provider.ModelDriver.(models.ASRDriver); !ok {
						t.Errorf("%s advertises transcription without an ASR driver", model.Name)
					}
				}
				if seenTypes["ocr"] {
					if _, ok := provider.ModelDriver.(models.OCRDriver); !ok {
						t.Errorf("%s advertises OCR without an OCR driver", model.Name)
					}
				}
			}
		})
	}
}
