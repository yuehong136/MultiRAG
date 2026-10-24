package entity

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestXinferenceCatalogAndFactory(t *testing.T) {
	manager, err := NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	for _, provider := range manager.Providers {
		if provider.Name == "Xinference" {
			if provider.URL["default"] != "" || provider.ModelDriver == nil || provider.ModelDriver.Name() != "xinference" {
				t.Fatal("Xinference must use its configured instance URL and real driver", provider)
			}
			return
		}
	}
	t.Fatal("Xinference missing from the real provider catalog")
}

func TestXinferenceDynamicURLDoesNotRelaxHostedCatalog(t *testing.T) {
	for _, name := range []string{"xInFeReNcE", "OpenAI"} {
		t.Run(name, func(t *testing.T) {
			directory := t.TempDir()
			if err := os.WriteFile(filepath.Join(directory, "provider.json"), []byte(`{"name":"`+name+`","models":[]}`), 0o600); err != nil {
				t.Fatal(err)
			}
			manager, err := NewProviderManager(directory)
			if name == "OpenAI" {
				if err == nil || !strings.Contains(err.Error(), "has no default URL") {
					t.Fatal("hosted provider without an endpoint must still fail", manager, err)
				}
			} else if err != nil || len(manager.Providers) != 1 || manager.Providers[0].ModelDriver.Name() != "xinference" {
				t.Fatal("dynamic local provider must be recognized without a fake default", manager, err)
			}
		})
	}
}
