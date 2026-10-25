package entity

import (
	"multirag/internal/entity/models"
	"os"
	"path/filepath"
	"testing"
)

func TestRegionalBedrockCatalogLoadsWithoutFakeURL(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	bedrock := manager.FindProvider("Bedrock")
	if bedrock == nil || len(bedrock.URL) != 0 || len(bedrock.Models) == 0 {
		t.Fatal("native region catalog lost", bedrock)
	}
	if _, ok := bedrock.ModelDriver.(*models.BedrockModel); !ok {
		t.Fatal("Bedrock factory not bound")
	}
	// The exception applies to this native regional protocol only.
	directory := t.TempDir()
	if err := os.WriteFile(filepath.Join(directory, "futurmix.json"), []byte(`{"name":"FuturMix","models":[]}`), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := NewProviderManager(directory); err == nil {
		t.Fatal("unrelated missing endpoint accepted")
	}
}
