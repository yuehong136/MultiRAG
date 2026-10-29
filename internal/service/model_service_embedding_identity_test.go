package service

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"multirag/internal/entity/models"
)

func TestResolvedEmbeddingIdentityBedrock(t *testing.T) {
	const name = "amazon.titan-embed-text-v2:0"
	resolve := func(credentialRegion, override string) EmbeddingModelIdentity {
		t.Helper()
		key := fmt.Sprintf(`{"auth_mode":"access_key_secret","bedrock_region":%q,"bedrock_ak":"private-identity-key","bedrock_sk":"private-identity-secret"}`, credentialRegion)
		api := &models.APIConfig{APIKey: &key}
		if override != "" {
			api.Region = &override
		}
		identity, err := resolvedEmbeddingIdentity(models.NewBedrockModel(nil, models.URLSuffix{}), "Bedrock", name, "", "default", api)
		if err != nil {
			t.Fatal("Bedrock service identity failed", err)
		}
		raw, err := json.Marshal(identity)
		if err != nil || strings.Contains(string(raw), "private-identity") || strings.Contains(string(raw), "bedrock_ak") || strings.Contains(string(raw), "bedrock_sk") {
			t.Fatal("serialized identity exposed credential material")
		}
		return identity
	}
	east, west := resolve("us-east-1", ""), resolve("eu-west-1", "")
	if east == west || east.Provider != "bedrock" || east.Model != name || east.Region != "us-east-1" || west.Region != "eu-west-1" || east.Endpoint != "https://bedrock-runtime.us-east-1.amazonaws.com/model/amazon.titan-embed-text-v2:0/invoke" || west.Endpoint != "https://bedrock-runtime.eu-west-1.amazonaws.com/model/amazon.titan-embed-text-v2:0/invoke" {
		t.Fatal("effective regions or endpoints collapsed", east, west)
	}
	if resolve("us-east-1", "eu-west-1") != west || resolve("us-east-1", "default") != east {
		t.Fatal("service identity ignored effective API region")
	}
	for _, endpoint := range []string{
		"https://user:private-identity-secret@example.invalid/embedding",
		"https://example.invalid/embedding?token=private-identity-secret",
		"https://example.invalid/embedding#private-identity-secret",
	} {
		identity, err := resolvedEmbeddingIdentity(models.NewDummyModel(nil, models.URLSuffix{}), "OpenAI", "m", endpoint, "default", nil)
		if err == nil || identity != (EmbeddingModelIdentity{}) || strings.Contains(err.Error(), "private-identity") {
			t.Fatal("unsafe identity endpoint was not rejected privately")
		}
	}
}
