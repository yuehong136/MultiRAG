package models

import (
	"fmt"
	"net/http"
	"os"
	"strings"
	"sync/atomic"
	"testing"
)

type bedrockIdentityNoHTTPTransport struct{ calls atomic.Int64 }

func (b *bedrockIdentityNoHTTPTransport) RoundTrip(_ *http.Request) (*http.Response, error) {
	b.calls.Add(1)
	return nil, fmt.Errorf("unexpected Bedrock identity network I/O")
}

func TestBedrockEmbeddingIdentityEffectiveRegionAndEndpoint(t *testing.T) {
	const modelName = "amazon.titan-embed-text-v2:0"
	for _, test := range []struct {
		name, credentialRegion, override, wantRegion, wantEndpoint string
		urls                                                       map[string]string
		suffix                                                     URLSuffix
	}{
		{name: "credential region", credentialRegion: "us-east-1", wantRegion: "us-east-1", wantEndpoint: "https://bedrock-runtime.us-east-1.amazonaws.com/model/amazon.titan-embed-text-v2:0/invoke"},
		{name: "another credential region", credentialRegion: "eu-west-1", wantRegion: "eu-west-1", wantEndpoint: "https://bedrock-runtime.eu-west-1.amazonaws.com/model/amazon.titan-embed-text-v2:0/invoke"},
		{name: "default override keeps credential region", credentialRegion: "us-east-1", override: "default", wantRegion: "us-east-1", wantEndpoint: "https://bedrock-runtime.us-east-1.amazonaws.com/model/amazon.titan-embed-text-v2:0/invoke"},
		{name: "explicit API region", credentialRegion: "us-east-1", override: "eu-west-1", wantRegion: "eu-west-1", wantEndpoint: "https://bedrock-runtime.eu-west-1.amazonaws.com/model/amazon.titan-embed-text-v2:0/invoke"},
		{name: "China domain", credentialRegion: "cn-north-1", wantRegion: "cn-north-1", wantEndpoint: "https://bedrock-runtime.cn-north-1.amazonaws.com.cn/model/amazon.titan-embed-text-v2:0/invoke"},
		{name: "default endpoint override", credentialRegion: "us-east-1", wantRegion: "us-east-1", urls: map[string]string{"default": "https://default.invalid/runtime/"}, wantEndpoint: "https://default.invalid/runtime/model/amazon.titan-embed-text-v2:0/invoke"},
		{name: "region endpoint wins", credentialRegion: "us-east-1", override: "eu-west-1", wantRegion: "eu-west-1", urls: map[string]string{"default": "https://default.invalid", "eu-west-1": "https://regional.invalid/runtime/", "control:eu-west-1": "https://control.invalid"}, wantEndpoint: "https://regional.invalid/runtime/model/amazon.titan-embed-text-v2:0/invoke"},
		{name: "configured operation", credentialRegion: "us-east-1", wantRegion: "us-east-1", suffix: URLSuffix{Embedding: "/custom-invoke/"}, wantEndpoint: "https://bedrock-runtime.us-east-1.amazonaws.com/model/amazon.titan-embed-text-v2:0/custom-invoke"},
	} {
		t.Run(test.name, func(t *testing.T) {
			key := fmt.Sprintf(`{"auth_mode":"access_key_secret","bedrock_region":%q,"bedrock_ak":"private-identity-key","bedrock_sk":"private-identity-secret"}`, test.credentialRegion)
			originalKey, originalOverride := key, test.override
			api := &APIConfig{APIKey: &key}
			if test.override != "" {
				api.Region = &test.override
			}
			model := NewBedrockModel(test.urls, test.suffix)
			transport := &bedrockIdentityNoHTTPTransport{}
			model.httpClient.Transport = transport
			endpoint, region, err := model.ResolveEmbeddingEndpoint(modelName, api)
			if err != nil || endpoint != test.wantEndpoint || region != test.wantRegion || transport.calls.Load() != 0 {
				t.Fatal("identity does not match effective Embed route", endpoint, region, err, transport.calls.Load())
			}
			if strings.Contains(endpoint, "private-identity") || strings.Contains(region, "private-identity") || key != originalKey || api.Region != nil && *api.Region != originalOverride {
				t.Fatal("identity leaked credentials or changed caller configuration")
			}
		})
	}
}

func TestBedrockEmbeddingIdentityDoesNotLoadCredentialProviders(t *testing.T) {
	// A nonexistent selected profile and empty static environment would make
	// credential resolution fail. Pure route resolution must remain successful.
	for _, key := range []string{"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI"} {
		t.Setenv(key, "")
	}
	t.Setenv("AWS_CONFIG_FILE", os.DevNull)
	t.Setenv("AWS_SHARED_CREDENTIALS_FILE", os.DevNull)
	t.Setenv("AWS_PROFILE", "identity-must-not-load-this-profile")
	t.Setenv("AWS_EC2_METADATA_DISABLED", "true")
	for _, key := range []string{
		`{"auth_mode":"assume_role","bedrock_region":"us-east-1"}`,
		`{"auth_mode":"iam_role","bedrock_region":"us-east-1","aws_role_arn":"arn:aws:iam::123456789012:role/identity-only"}`,
	} {
		model := NewBedrockModel(nil, URLSuffix{})
		transport := &bedrockIdentityNoHTTPTransport{}
		model.httpClient.Transport = transport
		endpoint, region, err := model.ResolveEmbeddingEndpoint("cohere.embed-english-v3", &APIConfig{APIKey: &key})
		if err != nil || endpoint != "https://bedrock-runtime.us-east-1.amazonaws.com/model/cohere.embed-english-v3/invoke" || region != "us-east-1" || transport.calls.Load() != 0 {
			t.Fatal("read-only identity attempted credential resolution", endpoint, region, err, transport.calls.Load())
		}
	}
}

func TestBedrockEmbeddingIdentityCredentialAndEndpointFailures(t *testing.T) {
	valid := `{"auth_mode":"access_key_secret","bedrock_region":"us-east-1","bedrock_ak":"private-identity-key","bedrock_sk":"private-identity-secret"}`
	for _, test := range []struct {
		name, key, override, wantError string
		urls                           map[string]string
		suffix                         URLSuffix
	}{
		{name: "empty key", wantError: "bedrock: API key JSON is required"},
		{name: "malformed key", key: "private-identity-secret", wantError: "bedrock: invalid credential JSON schema"},
		{name: "duplicate key", key: `{"auth_mode":"assume_role","bedrock_region":"us-east-1","bedrock_region":"eu-west-1"}`, wantError: "bedrock: invalid credential JSON schema"},
		{name: "wrong schema", key: `{"auth_mode":7,"bedrock_region":"us-east-1"}`, wantError: "bedrock: invalid credential JSON schema"},
		{name: "oversized key", key: strings.Repeat("private-identity-secret", 4000), wantError: "bedrock: credential JSON exceeds limit"},
		{name: "unknown mode", key: `{"auth_mode":"private-identity-secret","bedrock_region":"us-east-1"}`, wantError: "bedrock: invalid auth_mode"},
		{name: "missing secret", key: `{"auth_mode":"access_key_secret","bedrock_region":"us-east-1","bedrock_ak":"private-identity-key"}`, wantError: "bedrock: bedrock_ak and bedrock_sk are required"},
		{name: "invalid role", key: `{"auth_mode":"iam_role","bedrock_region":"us-east-1","aws_role_arn":"private-identity-secret"}`, wantError: "bedrock: iam_role requires aws_role_arn"},
		{name: "invalid credential region", key: `{"auth_mode":"assume_role","bedrock_region":"private-identity-secret"}`, wantError: "bedrock: a valid AWS region is required"},
		{name: "invalid explicit region", key: valid, override: "private-identity-secret", wantError: "bedrock: a valid AWS region is required"},
		{name: "unsafe endpoint", key: valid, urls: map[string]string{"default": "https://user:private-identity-secret@example.invalid"}, wantError: "bedrock: invalid endpoint"},
		{name: "unsafe operation", key: valid, suffix: URLSuffix{Embedding: "invoke?private-identity-secret"}, wantError: "bedrock: invalid operation suffix"},
	} {
		t.Run(test.name, func(t *testing.T) {
			model := NewBedrockModel(test.urls, test.suffix)
			transport := &bedrockIdentityNoHTTPTransport{}
			model.httpClient.Transport = transport
			api := &APIConfig{APIKey: &test.key}
			if test.override != "" {
				api.Region = &test.override
			}
			endpoint, region, err := model.ResolveEmbeddingEndpoint("amazon.titan-embed-text-v2:0", api)
			if err == nil || err.Error() != test.wantError || endpoint != "" || region != "" || transport.calls.Load() != 0 {
				t.Fatal("invalid identity was not rejected privately", endpoint, region, err, transport.calls.Load())
			}
			if strings.Contains(err.Error(), "private-identity") {
				t.Fatal("credential failure leaked source material")
			}
		})
	}
	if endpoint, region, err := NewBedrockModel(nil, URLSuffix{}).ResolveEmbeddingEndpoint("amazon.titan-embed-text-v2:0", nil); err == nil || endpoint != "" || region != "" {
		t.Fatal("nil credential config accepted")
	}
}
