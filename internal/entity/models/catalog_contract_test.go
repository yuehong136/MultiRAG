package models

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestCanonicalCatalogProvidersModelsAndAliases(t *testing.T) {
	if err := InitProviderManager(filepath.Join(findRepoRoot(), "conf", "models")); err != nil {
		t.Fatal(err)
	}
	pm := GetProviderManager()
	for _, provider := range pm.Providers {
		t.Run(provider.Name, func(t *testing.T) {
			if provider.ModelDriver == nil || pm.FindProvider(strings.ToUpper(provider.Name)) == nil {
				t.Fatal("provider registration or case insensitive identity lost")
			}
			for _, model := range provider.Models {
				got, err := pm.GetModelByName(provider.Name, model.Name)
				if err != nil || got.Name != model.Name || got.Class == nil || *got.Class == "" {
					t.Fatalf("model %s: %v", model.Name, err)
				}
				for _, typ := range model.ModelTypes {
					if !got.ModelTypeMap[typ] {
						t.Fatalf("%s missing capability %s", model.Name, typ)
					}
				}
			}
		})
	}
	for _, model := range pm.AllModels {
		for _, alias := range append([]string{model.Name}, model.Alias...) {
			if strings.TrimSpace(alias) == "" {
				continue
			}
			got := pm.GetModelByNameOrAlias(strings.ToUpper(strings.TrimSpace(alias)))
			if got == nil || got.Name != model.Name {
				t.Fatalf("alias %q lost", alias)
			}
		}
	}
	for _, name := range []string{"MinerU.Net", "MinerU", "PaddleOCR.Net", "PaddleOCR", "LocalAI", "Xinference", "Voyage", "Bedrock"} {
		if pm.FindProvider(name) == nil {
			t.Fatalf("canonical provider %q absent", name)
		}
	}
}

func TestCanonicalCatalogTypedProjectionDoesNotMutateMetadata(t *testing.T) {
	if err := InitProviderManager(filepath.Join(findRepoRoot(), "conf", "models")); err != nil {
		t.Fatal(err)
	}
	pm := GetProviderManager()
	before, err := json.Marshal(pm.AllModels)
	if err != nil {
		t.Fatal(err)
	}
	for _, model := range pm.AllModels {
		rows := ParseListModel(ModelList{Models: []DSModel{{ID: model.Name, OwnedBy: "owner"}}})
		if len(rows) != 1 || rows[0].Name != model.Name+"@owner" {
			t.Fatal("discovery changed external identity")
		}
		row := rows[0]
		if !reflect.DeepEqual(row.MaxTokens, model.MaxTokens) || !reflect.DeepEqual(row.ModelTypes, model.ModelTypes) || !reflect.DeepEqual(row.Thinking, model.Thinking) || !reflect.DeepEqual(row.MaxDimension, model.MaxDimension) || !reflect.DeepEqual(row.Dimensions, model.Dimensions) {
			t.Fatalf("metadata lost for %s", model.Name)
		}
		if row.MaxTokens != nil {
			*row.MaxTokens = -1
		}
		if row.MaxDimension != nil {
			*row.MaxDimension = -1
		}
		if len(row.ModelTypes) > 0 {
			row.ModelTypes[0] = "mutated"
		}
		if len(row.Dimensions) > 0 {
			row.Dimensions[0] = -1
		}
		if row.Thinking != nil {
			row.Thinking.DefaultValue = !row.Thinking.DefaultValue
		}
	}
	unknown := ParseListModel(ModelList{Models: []DSModel{{ID: "unknown-provider-model", OwnedBy: "owner"}}})
	if len(unknown) != 1 || unknown[0].Name != "unknown-provider-model@owner" || unknown[0].MaxTokens != nil || unknown[0].ModelTypes != nil {
		t.Fatal("unknown inventory fabricated metadata")
	}
	after, _ := json.Marshal(pm.AllModels)
	if string(before) != string(after) {
		t.Fatal("discovery output mutated global catalog")
	}
}

func TestCanonicalCatalogPublicationIsAtomic(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "conf", "models"), 0700); err != nil {
		t.Fatal(err)
	}
	for name, data := range map[string]string{"go.mod": "module fixture\n", "conf/all_models.json": `{"models":[{"name":"fixture","alias":["CaseAlias"],"max_tokens":42,"model_types":["chat"]}]}`, "conf/models/openai.json": `{"name":"OpenAI","url":{"default":"https://example.invalid"},"models":[{"name":"plain","model_types":["chat"]}]}`} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
	}
	t.Chdir(dir)
	if err := InitProviderManager("conf/models"); err != nil {
		t.Fatal(err)
	}
	before := GetProviderManager()
	for _, invalid := range []string{`{"models":[{"name":"fixture","max_tokens":1.2}]}`, `{"models":[{"name":"fixture","alias":["collision"]},{"name":"other","alias":["COLLISION"]}]}`, `{"models":[`} {
		if err := os.WriteFile("conf/all_models.json", []byte(invalid), 0600); err != nil {
			t.Fatal(err)
		}
		if err := InitProviderManager("conf/models"); err == nil {
			t.Fatal("invalid catalog published")
		}
		if GetProviderManager() != before || before.GetModelByNameOrAlias("CASEALIAS") == nil {
			t.Fatal("failed load replaced catalog")
		}
	}
	if err := os.WriteFile("conf/models/openai.json", []byte(`{"name":"OpenAI","url_suffix":{"unsupported":"route"}}`), 0600); err != nil {
		t.Fatal(err)
	}
	if err := InitProviderManager("conf/models"); err == nil || GetProviderManager() != before {
		t.Fatal("unknown suffix published")
	}
}
