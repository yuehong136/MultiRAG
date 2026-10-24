package entity

import (
	"multirag/internal/entity/models"
	"path/filepath"
	"testing"
)

func TestMinerULocalCatalogLoadsWithoutGlobalEndpoint(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal("real catalog directory failed to load", err)
	}
	provider := manager.FindProvider("mineru_local")
	if provider == nil || provider.ModelDriver.Name() != "mineru_local" || provider.URL["default"] != "" || provider.Class != "local" || provider.URLSuffix.DocumentParse != "file_parse" || provider.URLSuffix.Task != "tasks" {
		t.Fatal("local parser catalog contract missing", provider)
	}
	if _, ok := provider.ModelDriver.(models.FileParser); !ok {
		t.Fatal("local parser factory fell back to dummy")
	}
}
