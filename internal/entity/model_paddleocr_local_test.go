package entity

import (
	"path/filepath"
	"testing"

	"multirag/internal/entity/models"
)

func TestPaddleOCRLocalCatalogRequiresTenantEndpoint(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal("real catalog failed to load", err)
	}
	provider := manager.FindProvider("PaddleOCR_Local")
	if provider == nil || provider.URL["default"] != "" || provider.URLSuffix.OCR != "layout-parsing" || provider.ModelDriver.Name() != "paddleocr_local" {
		t.Fatal("local OCR catalog must require a tenant endpoint", provider)
	}
	if _, ok := provider.ModelDriver.(models.OCRDriver); !ok {
		t.Fatal("local OCR factory returned an unsupported driver")
	}
	if _, ok := provider.ModelDriver.(models.OCRURLDriver); ok {
		t.Fatal("local OCR cannot fetch caller URLs")
	}
}
