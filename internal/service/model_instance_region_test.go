package service

import (
	"reflect"
	"testing"

	"multirag/internal/entity"
	"multirag/internal/entity/models"
)

type instanceRegionFactory struct {
	calls int
	urls  map[string]string
}

func (f *instanceRegionFactory) CreateModelDriver(name string, urls map[string]string, suffix models.URLSuffix) (models.ModelDriver, error) {
	f.calls++
	f.urls = urls
	return models.NewModelFactory().CreateModelDriver(name, urls, suffix)
}

func TestInstanceModelDriverRejectsUnknownRegionBeforeConstruction(t *testing.T) {
	provider := &entity.Provider{Name: "vllm", URL: map[string]string{"default": "https://default.invalid/v1", "eu": "https://eu.invalid/v1"}}
	for _, region := range []string{"unknown", "US", "missing"} {
		t.Run(region, func(t *testing.T) {
			raw, err := encodeProviderInstanceExtra(region, "")
			if err != nil {
				t.Fatal(err)
			}
			factory := &instanceRegionFactory{}
			driver, config, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", Extra: raw}, factory)
			if err == nil || driver != nil || config != nil || factory.calls != 0 {
				t.Fatalf("unknown region reached construction: driver=%T config=%v err=%v calls=%d", driver, config, err, factory.calls)
			}
		})
	}
}

func TestInstanceModelDriverUsesSelectedRegionOrExplicitEndpoint(t *testing.T) {
	original := map[string]string{"default": "https://default.invalid/v1", "eu": "https://eu.invalid/v1"}
	provider := &entity.Provider{Name: "vllm", URL: original}
	for _, test := range []struct {
		name, region, override, wantURL, wantRegion string
	}{
		{"omitted", "", "", original["default"], "default"},
		{"default", "default", "", original["default"], "default"},
		{"selected", "eu", "", original["eu"], "eu"},
		{"override", "custom-region", "https://instance.invalid/v1", "https://instance.invalid/v1", "custom-region"},
	} {
		t.Run(test.name, func(t *testing.T) {
			raw, err := encodeProviderInstanceExtra(test.region, test.override)
			if err != nil {
				t.Fatal(err)
			}
			factory := &instanceRegionFactory{}
			_, config, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", Extra: raw}, factory)
			if err != nil || factory.calls != 1 || config == nil || *config.Region != test.wantRegion || factory.urls["default"] != test.wantURL || factory.urls[test.wantRegion] != test.wantURL {
				t.Fatalf("wrong selected instance: urls=%v config=%v err=%v calls=%d", factory.urls, config, err, factory.calls)
			}
		})
	}
	if !reflect.DeepEqual(provider.URL, map[string]string{"default": "https://default.invalid/v1", "eu": "https://eu.invalid/v1"}) {
		t.Fatal("instance resolution mutated the provider catalog")
	}
}
