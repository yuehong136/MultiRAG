package service

import (
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"testing"
)

func TestBedrockInstanceAllowsNativeRegionalEndpoints(t *testing.T) {
	provider := &entity.Provider{Name: "Bedrock"}
	key := `{"auth_mode":"access_key_secret","bedrock_ak":"controlled-key","bedrock_sk":"controlled-secret","bedrock_region":"us-east-1"}`
	driver, api, err := instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", APIKey: key, Extra: `{}`})
	if err != nil || len(driver.(*models.BedrockModel).BaseURL) != 0 || *api.Region != "default" {
		t.Fatal(driver, api, err)
	}
	// A loopback instance override remains local to that instance and both planes.
	driver, api, err = instanceModelDriver(provider, &entity.TenantModelInstance{Status: "active", APIKey: key, Extra: `{"region":"eu-west-1","base_url":"http://127.0.0.1:12345"}`})
	if err != nil || driver.(*models.BedrockModel).BaseURL["default"] != "http://127.0.0.1:12345" || *api.Region != "eu-west-1" || len(provider.URL) != 0 {
		t.Fatal(driver, api, err)
	}
	if _, _, err := instanceModelDriver(&entity.Provider{Name: "FuturMix"}, &entity.TenantModelInstance{Status: "active", APIKey: "controlled"}); err == nil {
		t.Fatal("missing endpoint allowed for unrelated driver")
	}
	if _, _, err := instanceModelDriver(provider, nil); err == nil {
		t.Fatal("nil instance accepted")
	}
}
