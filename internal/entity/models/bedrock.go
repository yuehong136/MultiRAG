package models

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"maps"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	v4 "github.com/aws/aws-sdk-go-v2/aws/signer/v4"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/sts"
)

// BedrockModel uses signed Converse requests and binary event streams.
// Credentials, region and endpoint selection belong to each call/instance.
type BedrockModel struct {
	*DummyModel
	httpClient        *http.Client
	streamIdleTimeout time.Duration
}

func NewBedrockModel(urls map[string]string, suffix URLSuffix) *BedrockModel {
	return &BedrockModel{DummyModel: NewDummyModel(maps.Clone(urls), suffix), httpClient: &http.Client{Transport: openAIChatTransport, CheckRedirect: rejectModelRedirect}, streamIdleTimeout: 60 * time.Second}
}
func (b *BedrockModel) Name() string { return "bedrock" }

type bedrockKey struct {
	AuthMode     string `json:"auth_mode"`
	Region       string `json:"bedrock_region"`
	AccessKey    string `json:"bedrock_ak"`
	SecretKey    string `json:"bedrock_sk"`
	SessionToken string `json:"bedrock_session_token"`
	RoleARN      string `json:"aws_role_arn"`
	ExternalID   string `json:"aws_external_id"`
	SessionName  string `json:"role_session_name"`
}

var bedrockRegionPattern = regexp.MustCompile(`^[a-z]{2}(-[a-z]+)+-[0-9]+$`)

func parseBedrockKey(api *APIConfig) (*bedrockKey, string, error) {
	if api == nil || api.APIKey == nil || strings.TrimSpace(*api.APIKey) == "" {
		return nil, "", fmt.Errorf("bedrock: API key JSON is required")
	}
	var key bedrockKey
	if len(*api.APIKey) > 65536 {
		return nil, "", fmt.Errorf("bedrock: credential JSON exceeds limit")
	}
	if err := decodeBedrockJSON(strings.NewReader(*api.APIKey), &key); err != nil {
		return nil, "", fmt.Errorf("bedrock: invalid credential JSON schema")
	}
	switch key.AuthMode {
	case "access_key_secret":
		if strings.TrimSpace(key.AccessKey) == "" || strings.TrimSpace(key.SecretKey) == "" {
			return nil, "", fmt.Errorf("bedrock: bedrock_ak and bedrock_sk are required")
		}
	case "iam_role":
		if !strings.HasPrefix(key.RoleARN, "arn:") || !strings.Contains(key.RoleARN, ":iam::") || !strings.Contains(key.RoleARN, ":role/") {
			return nil, "", fmt.Errorf("bedrock: iam_role requires aws_role_arn")
		}
		if (key.AccessKey == "") != (key.SecretKey == "") {
			return nil, "", fmt.Errorf("bedrock: incomplete source credentials")
		}
	case "assume_role": // Matches the Python contract: use the default AWS credential chain.
	default:
		return nil, "", fmt.Errorf("bedrock: invalid auth_mode")
	}
	region := key.Region
	if api.Region != nil && *api.Region != "" && *api.Region != "default" {
		region = *api.Region
	}
	if !bedrockRegionPattern.MatchString(region) {
		return nil, "", fmt.Errorf("bedrock: a valid AWS region is required")
	}
	return &key, region, nil
}
func (b *BedrockModel) resolveCredentials(ctx context.Context, key *bedrockKey, region string) (aws.Credentials, error) {
	if err := ctx.Err(); err != nil {
		return aws.Credentials{}, err
	}
	if key.AuthMode == "access_key_secret" {
		return aws.Credentials{AccessKeyID: key.AccessKey, SecretAccessKey: key.SecretKey, SessionToken: key.SessionToken, Source: "multirag/bedrock/static"}, nil
	}
	opts := []func(*config.LoadOptions) error{config.WithRegion(region), config.WithHTTPClient(b.httpClient), config.WithRetryMaxAttempts(1)}
	if key.AuthMode == "iam_role" && key.AccessKey != "" {
		opts = append(opts, config.WithCredentialsProvider(credentials.NewStaticCredentialsProvider(key.AccessKey, key.SecretKey, key.SessionToken)))
	}
	cfg, err := config.LoadDefaultConfig(ctx, opts...)
	if err != nil {
		return aws.Credentials{}, fmt.Errorf("bedrock: AWS config unavailable")
	}
	if key.AuthMode == "assume_role" {
		value, err := cfg.Credentials.Retrieve(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return aws.Credentials{}, ctx.Err()
			}
			return aws.Credentials{}, fmt.Errorf("bedrock: AWS credentials unavailable")
		}
		return value, nil
	}
	client := sts.NewFromConfig(cfg, func(o *sts.Options) {
		if endpoint := b.BaseURL["sts:"+region]; endpoint != "" {
			o.BaseEndpoint = &endpoint
		}
	})
	session := key.SessionName
	if session == "" {
		session = "BedrockSession"
	}
	input := &sts.AssumeRoleInput{RoleArn: &key.RoleARN, RoleSessionName: &session}
	if key.ExternalID != "" {
		input.ExternalId = &key.ExternalID
	}
	out, err := client.AssumeRole(ctx, input)
	if err != nil {
		if ctx.Err() != nil {
			return aws.Credentials{}, ctx.Err()
		}
		return aws.Credentials{}, fmt.Errorf("bedrock: STS AssumeRole failed")
	}
	if out == nil || out.Credentials == nil || aws.ToString(out.Credentials.AccessKeyId) == "" || aws.ToString(out.Credentials.SecretAccessKey) == "" || aws.ToString(out.Credentials.SessionToken) == "" || out.Credentials.Expiration == nil || !out.Credentials.Expiration.After(time.Now()) {
		return aws.Credentials{}, fmt.Errorf("bedrock: invalid STS credentials")
	}
	c := out.Credentials
	return aws.Credentials{AccessKeyID: *c.AccessKeyId, SecretAccessKey: *c.SecretAccessKey, SessionToken: *c.SessionToken, CanExpire: true, Expires: *c.Expiration, Source: "multirag/bedrock/iam_role"}, nil
}
func (b *BedrockModel) endpoint(region, name, operation string, control bool) (string, error) {
	base := b.BaseURL[region]
	if base == "" {
		base = b.BaseURL["default"]
	}
	if control && b.BaseURL["control:"+region] != "" {
		base = b.BaseURL["control:"+region]
	}
	if base == "" {
		host := "bedrock-runtime"
		if control {
			host = "bedrock"
		}
		domain := "amazonaws.com"
		if strings.HasPrefix(region, "cn-") {
			domain += ".cn"
		}
		base = "https://" + host + "." + region + "." + domain
	}
	parsed, err := url.Parse(base)
	if err != nil || parsed.Host == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return "", fmt.Errorf("bedrock: invalid endpoint")
	}
	if strings.ContainsAny(operation, "?#\\") || strings.Contains(operation, "..") {
		return "", fmt.Errorf("bedrock: invalid operation suffix")
	}
	if control {
		return strings.TrimRight(base, "/") + "/" + strings.Trim(operation, "/"), nil
	}
	if strings.TrimSpace(name) == "" || strings.TrimSpace(name) != name {
		return "", fmt.Errorf("bedrock: model id is required")
	}
	return strings.TrimRight(base, "/") + "/model/" + url.PathEscape(name) + "/" + strings.Trim(operation, "/"), nil
}
func bedrockSuffix(configured, otherwise string) string {
	if configured != "" {
		return configured
	}
	return otherwise
}

// Both control and runtime use the SigV4 signing name "bedrock".
// The endpoint hostname is different from the signing service name.
func (b *BedrockModel) request(api *APIConfig, name, operation string, body any, stream, control bool) (*http.Response, error) {
	key, region, err := parseBedrockKey(api)
	if err != nil {
		return nil, err
	}
	endpoint, err := b.endpoint(region, name, operation, control)
	if err != nil {
		return nil, err
	}
	var raw []byte
	if body != nil {
		raw, err = json.Marshal(body)
		if err != nil {
			return nil, err
		}
	}
	// Credential providers may perform I/O even for a long-lived stream.
	ctx, cancel := context.WithTimeout(requestContext(api), 120*time.Second)
	creds, err := b.resolveCredentials(ctx, key, region)
	cancel()
	if err != nil {
		return nil, err
	}
	return b.signedRequest(requestContext(api), endpoint, raw, creds, region, stream, control)
}

func (b *BedrockModel) signedRequest(ctx context.Context, endpoint string, raw []byte, creds aws.Credentials, region string, stream, control bool) (*http.Response, error) {
	method := http.MethodPost
	if control {
		method = http.MethodGet
	}
	req, err := http.NewRequestWithContext(ctx, method, endpoint, bytes.NewReader(raw))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	accept := "application/json"
	if stream {
		accept = "application/vnd.amazon.eventstream"
	}
	req.Header.Set("Accept", accept)
	hash := sha256.Sum256(raw)
	if err := v4.NewSigner().SignHTTP(ctx, creds, req, hex.EncodeToString(hash[:]), "bedrock", region, time.Now().UTC()); err != nil {
		return nil, fmt.Errorf("bedrock: sign request: %w", err)
	}
	resp, err := b.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("bedrock request: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("bedrock: HTTP %d", resp.StatusCode)
	}
	return resp, nil
}

func bedrockChatBody(name string, messages []Message, config *ChatConfig) (map[string]any, error) {
	if strings.TrimSpace(name) == "" {
		return nil, fmt.Errorf("bedrock: model id is required")
	}
	if err := validateHostedGeneration(config); err != nil {
		return nil, err
	}
	if config != nil && (config.Thinking != nil && *config.Thinking || config.Vision != nil && *config.Vision) {
		return nil, fmt.Errorf("bedrock: thinking and vision options are unsupported")
	}
	if err := ValidateMessages(messages); err != nil {
		return nil, err
	}
	var system, history []map[string]any
	for _, message := range messages {
		text, ok := message.Content.(string)
		if !ok {
			return nil, fmt.Errorf("bedrock: only text chat is supported")
		}
		if message.ReasoningContent != nil && *message.ReasoningContent != "" {
			return nil, fmt.Errorf("bedrock: reasoning history is unsupported")
		}
		if message.Role == "system" {
			if len(history) > 0 {
				return nil, fmt.Errorf("bedrock: system messages must precede conversation")
			}
			system = append(system, map[string]any{"text": text})
			continue
		}
		role := message.Role
		if role == "model" {
			role = "assistant"
		}
		if role != "assistant" && role != "user" {
			return nil, fmt.Errorf("bedrock: unsupported role")
		}
		history = append(history, map[string]any{"role": role, "content": []map[string]any{{"text": text}}})
	}
	if len(history) == 0 {
		return nil, fmt.Errorf("bedrock: conversation is required")
	}
	body := map[string]any{"messages": history}
	if len(system) > 0 {
		body["system"] = system
	}
	if config != nil {
		inf := map[string]any{}
		if config.MaxTokens != nil {
			inf["maxTokens"] = *config.MaxTokens
		}
		if config.Temperature != nil {
			inf["temperature"] = *config.Temperature
		}
		if config.TopP != nil {
			inf["topP"] = *config.TopP
		}
		if config.Stop != nil {
			inf["stopSequences"] = *config.Stop
		}
		applyClaudeSamplingPolicy(name, inf)
		if len(inf) > 0 {
			body["inferenceConfig"] = inf
		}
	}
	return body, nil
}
func validBedrockStop(reason string) error {
	switch reason {
	case "end_turn", "max_tokens", "stop_sequence":
		return nil
	}
	return fmt.Errorf("bedrock: missing, failed or unsupported stop reason")
}
func (b *BedrockModel) ChatWithMessages(name string, api *APIConfig, messages []Message, config *ChatConfig) (*ChatResponse, error) {
	body, err := bedrockChatBody(name, messages, config)
	if err != nil {
		return nil, err
	}
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := b.request(local, name, bedrockSuffix(b.URLSuffix.Chat, "converse"), body, false, false)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var raw struct {
		Output struct {
			Message struct {
				Role    string                       `json:"role"`
				Content []map[string]json.RawMessage `json:"content"`
			} `json:"message"`
		} `json:"output"`
		StopReason string          `json:"stopReason"`
		Error      json.RawMessage `json:"error"`
	}
	if err := decodeBoundedModelJSON(resp.Body, &raw); err != nil {
		return nil, fmt.Errorf("bedrock: invalid Converse response")
	}
	if len(raw.Error) > 0 && string(raw.Error) != "null" {
		return nil, fmt.Errorf("bedrock: provider error")
	}
	if err := validBedrockStop(raw.StopReason); err != nil {
		return nil, err
	}
	if raw.Output.Message.Role != "assistant" || len(raw.Output.Message.Content) == 0 {
		return nil, fmt.Errorf("bedrock: missing assistant response")
	}
	var text strings.Builder
	for _, block := range raw.Output.Message.Content {
		var value *string
		if len(block) != 1 || json.Unmarshal(block["text"], &value) != nil || value == nil {
			return nil, fmt.Errorf("bedrock: unsupported or missing content block")
		}
		text.WriteString(*value)
	}
	answer := text.String()
	result := &ChatResponse{Answer: &answer}
	if strings.TrimSpace(answer) == "" {
		return nil, fmt.Errorf("bedrock: empty answer")
	}
	if err := ValidateChatResponse(result); err != nil {
		return nil, err
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return result, nil
}
func (b *BedrockModel) ListModels(api *APIConfig) ([]string, error) {
	local, cancel := modelCallConfig(api)
	defer cancel()
	resp, err := b.request(local, "", bedrockSuffix(b.URLSuffix.Models, "foundation-models"), nil, false, true)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var raw struct {
		Models *[]struct {
			ID string `json:"modelId"`
		} `json:"modelSummaries"`
		Error json.RawMessage `json:"error"`
	}
	if err := decodeBoundedModelJSON(resp.Body, &raw); err != nil {
		return nil, fmt.Errorf("bedrock: invalid model catalog")
	}
	if raw.Models == nil || len(raw.Error) > 0 && string(raw.Error) != "null" {
		return nil, fmt.Errorf("bedrock: missing model catalog or provider error")
	}
	names := make([]string, 0, len(*raw.Models))
	seen := map[string]bool{}
	for _, item := range *raw.Models {
		if strings.TrimSpace(item.ID) == "" || strings.TrimSpace(item.ID) != item.ID || seen[item.ID] {
			return nil, fmt.Errorf("bedrock: invalid or duplicate model id")
		}
		seen[item.ID] = true
		names = append(names, item.ID)
	}
	if err := requestContext(local).Err(); err != nil {
		return nil, err
	}
	return names, nil
}
func (b *BedrockModel) CheckConnection(api *APIConfig) error { _, err := b.ListModels(api); return err }

var _ ModelDriver = (*BedrockModel)(nil)
