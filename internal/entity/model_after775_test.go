package entity

import (
	"multirag/internal/entity/models"
	"path/filepath"
	"testing"
)

func TestAfter775ProviderCatalogs(t *testing.T) {
	pm, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"PPIO", "TokenPony", "Groq", "TogetherAI", "ZHIPU-AI", "OpenRouter"} {
		t.Run(name, func(t *testing.T) {
			p := pm.FindProvider(name)
			if p == nil || p.ModelDriver.Name() == "dummy" {
				t.Fatal("provider missing", name)
			}
			if len(p.Models) == 0 {
				t.Fatal("models missing")
			}
			for _, m := range p.Models {
				if len(m.ModelTypeMap) == 0 {
					t.Fatal("capability map missing", m.Name)
				}
			}
			if name == "PPIO" && len(p.Models) != 21 {
				t.Fatal("full PPIO catalog footprint", len(p.Models))
			}
			if name == "TogetherAI" || name == "ZHIPU-AI" || name == "OpenRouter" || name == "Groq" {
				if _, ok := p.ModelDriver.(models.ASRDriver); !ok || p.URLSuffix.ASR != "audio/transcriptions" {
					t.Fatal("ASR contract missing")
				}
			} else {
				if _, ok := p.ModelDriver.(models.ASRDriver); ok {
					t.Fatal("future capability exposed")
				}
			}
		})
	}
}
