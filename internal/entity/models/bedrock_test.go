package models

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/aws/protocol/eventstream"
	v4 "github.com/aws/aws-sdk-go-v2/aws/signer/v4"
)

const bedrockTestKey = `{"auth_mode":"access_key_secret","bedrock_region":"us-east-1","bedrock_ak":"AKIDEXAMPLE","bedrock_sk":"controlled-secret","bedrock_session_token":"controlled-token"}`

func bedrockTestAPI() *APIConfig { key := bedrockTestKey; return &APIConfig{APIKey: &key} }

func verifyBedrockSignature(t *testing.T, r *http.Request, body []byte, key, secret, token, region, service string) {
	t.Helper()
	original := r.Header.Get("Authorization")
	if !strings.Contains(original, "Credential="+key+"/") || !strings.Contains(original, "/"+region+"/"+service+"/aws4_request") || r.Header.Get("X-Amz-Security-Token") != token {
		t.Error("signature scope/token", original)
	}
	when, err := time.Parse("20060102T150405Z", r.Header.Get("X-Amz-Date"))
	if err != nil {
		t.Fatal(err)
	}
	copy := r.Clone(context.Background())
	copy.URL.Scheme = "http"
	copy.URL.Host = r.Host
	copy.Header.Del("Authorization")
	// The HTTP transport adds unsigned headers after signing (for example Accept-Encoding).
	// Reconstruct precisely the signed header set seen by the independent receiver.
	signed := strings.Split(strings.Split(original, "SignedHeaders=")[1], ",")[0]
	for header := range copy.Header {
		if !strings.Contains(";"+signed+";", ";"+strings.ToLower(header)+";") {
			copy.Header.Del(header)
		}
	}
	hash := sha256.Sum256(body)
	err = v4.NewSigner().SignHTTP(copy.Context(), aws.Credentials{AccessKeyID: key, SecretAccessKey: secret, SessionToken: token}, copy, hex.EncodeToString(hash[:]), service, region, when)
	if err != nil || copy.Header.Get("Authorization") != original {
		t.Error("signed body/header verification failed", err)
	}
}
func TestBedrockConverseAndCatalog(t *testing.T) {
	server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		verifyBedrockSignature(t, r, raw, "AKIDEXAMPLE", "controlled-secret", "controlled-token", "us-east-1", "bedrock")
		if r.Method == "GET" {
			if r.URL.Path != "/foundation-models" {
				t.Error(r.URL)
			}
			fmt.Fprint(w, `{"modelSummaries":[{"modelId":"m"}]}`)
			return
		}
		if r.URL.EscapedPath() != "/model/arn:aws:bedrock:us-east-1:123:inference-profile%2Fm/converse" {
			t.Error(r.URL.EscapedPath())
		}
		var body map[string]any
		if err := json.Unmarshal(raw, &body); err != nil {
			t.Error(err)
		}
		if !reflect.DeepEqual(body["system"], []any{map[string]any{"text": "rules"}}) {
			t.Error(body)
		}
		options, _ := body["inferenceConfig"].(map[string]any)
		if options["temperature"] != float64(0) || options["topP"] != float64(0) {
			t.Error(options)
		}
		fmt.Fprint(w, `{"output":{"message":{"role":"assistant","content":[{"text":"Hello "},{"text":"world"}]}},"stopReason":"end_turn"}`)
	}))
	defer server.Close()
	urls := map[string]string{"default": server.URL}
	driver := NewBedrockModel(urls, URLSuffix{})
	urls["default"] = "http://unused.invalid"
	zero := 0.0
	api := bedrockTestAPI()
	def := "default"
	api.Region = &def
	result, err := driver.ChatWithMessages("arn:aws:bedrock:us-east-1:123:inference-profile/m", api, []Message{{Role: "system", Content: "rules"}, {Role: "user", Content: "q"}}, &ChatConfig{Temperature: &zero, TopP: &zero})
	if err != nil || *result.Answer != "Hello world" {
		t.Fatal(result, err)
	}
	if names, err := driver.ListModels(api); err != nil || !reflect.DeepEqual(names, []string{"m"}) {
		t.Fatal(names, err)
	}
	if err := driver.CheckConnection(api); err != nil {
		t.Fatal(err)
	}
	if endpoint, err := NewBedrockModel(nil, URLSuffix{}).endpoint("cn-north-1", "m", "converse", false); err != nil || endpoint != "https://bedrock-runtime.cn-north-1.amazonaws.com.cn/model/m/converse" {
		t.Fatal(endpoint, err)
	}
}
func TestBedrockStrictRawResponses(t *testing.T) {
	for _, raw := range []string{`{}`, `null`, `{"output":{"message":{"role":"assistant","content":[{"text":""}]}},"stopReason":"end_turn"}`, `{"output":{"message":{"role":"user","content":[{"text":"answer"}]}},"stopReason":"end_turn"}`, `{"output":{"message":{"role":"assistant","content":[{"text":"answer"}]}}}`, `{"output":{"message":{"role":"assistant","content":[{"text":"answer"}]}},"stopReason":"tool_use"}`, `{"output":{"message":{"role":"assistant","content":[{"text":"answer"},{"toolUse":{}}]}},"stopReason":"end_turn"}`, `{"error":"private-secret"}`, `{} {}`} {
		server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, raw) }))
		driver := NewBedrockModel(map[string]string{"default": server.URL}, URLSuffix{})
		if result, err := driver.ChatWithMessages("m", bedrockTestAPI(), []Message{{Role: "user", Content: "q"}}, nil); err == nil || result != nil || strings.Contains(err.Error(), "private-secret") {
			t.Fatal(result, err)
		}
		server.Close()
	}
	for _, raw := range []string{`{}`, `{"modelSummaries":null}`, `{"modelSummaries":[{"modelId":" "}]}`, `{"modelSummaries":[{"modelId":"m"},{"modelId":"m"}]}`, `{"modelSummaries":[{}]}`, `{"modelSummaries":[],"error":"private-secret"}`, `{"modelSummaries":[]} {}`} {
		server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, raw) }))
		driver := NewBedrockModel(map[string]string{"default": server.URL}, URLSuffix{})
		if names, err := driver.ListModels(bedrockTestAPI()); err == nil || names != nil || strings.Contains(err.Error(), "private-secret") {
			t.Fatal(names, err)
		}
		server.Close()
	}
	server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, `{"modelSummaries":[]}`) }))
	defer server.Close()
	if names, err := NewBedrockModel(map[string]string{"default": server.URL}, URLSuffix{}).ListModels(bedrockTestAPI()); err != nil || names == nil || len(names) != 0 {
		t.Fatal(names, err)
	}
}
func TestBedrockInvalidInputsNoHTTP(t *testing.T) {
	var calls atomic.Int32
	server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1) }))
	defer server.Close()
	driver := NewBedrockModel(map[string]string{"default": server.URL}, URLSuffix{})
	for _, key := range []string{"", `null`, `{}`, `not-json`, `{"auth_mode":"unknown","bedrock_region":"us-east-1"}`, `{"auth_mode":"access_key_secret","bedrock_ak":"a","bedrock_sk":"b","bedrock_region":"../host"}`, `{"auth_mode":"access_key_secret","bedrock_ak":"a"}`, `{"auth_mode":"iam_role"}`} {
		if _, err := driver.ListModels(&APIConfig{APIKey: &key}); err == nil || strings.Contains(err.Error(), "not-json") {
			t.Fatal(err)
		}
	}
	for _, history := range [][]Message{nil, {{Role: "user", Content: " "}}, {{Role: "system", Content: "rules"}}, {{Role: "user", Content: "q"}, {Role: "system", Content: "rules"}}, multimodalFixture(), {{Role: "tool", Content: "q"}}} {
		if _, err := driver.ChatWithMessages("m", bedrockTestAPI(), history, nil); err == nil {
			t.Fatal("invalid history")
		}
	}
	if calls.Load() != 0 {
		t.Fatal("invalid inputs reached provider", calls.Load())
	}
}

type bedrockTestEvent struct{ kind, event, payload string }

func bedrockTestFrames(t *testing.T, events []bedrockTestEvent) []byte {
	t.Helper()
	var body bytes.Buffer
	for _, e := range events {
		kind := e.kind
		if kind == "" {
			kind = "event"
		}
		err := eventstream.NewEncoder().Encode(&body, eventstream.Message{Headers: eventstream.Headers{{Name: ":message-type", Value: eventstream.StringValue(kind)}, {Name: ":event-type", Value: eventstream.StringValue(e.event)}}, Payload: []byte(e.payload)})
		if err != nil {
			t.Fatal(err)
		}
	}
	return body.Bytes()
}
func bedrockValidEvents() []bedrockTestEvent {
	return []bedrockTestEvent{{event: "messageStart", payload: `{"role":"assistant"}`}, {event: "contentBlockDelta", payload: `{"contentBlockIndex":0,"delta":{"text":"answer"}}`}, {event: "contentBlockStop", payload: `{"contentBlockIndex":0}`}, {event: "messageStop", payload: `{"stopReason":"end_turn"}`}, {event: "metadata", payload: `{"usage":{"inputTokens":1,"outputTokens":1}}`}}
}
func TestBedrockBinaryEventStream(t *testing.T) {
	frames := bedrockTestFrames(t, bedrockValidEvents())
	server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		verifyBedrockSignature(t, r, raw, "AKIDEXAMPLE", "controlled-secret", "controlled-token", "us-east-1", "bedrock")
		if r.URL.Path != "/model/m/converse-stream" || r.Header.Get("Accept") != "application/vnd.amazon.eventstream" {
			t.Error(r.URL, r.Header)
		}
		w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
		_, _ = w.Write(frames)
	}))
	defer server.Close()
	driver := NewBedrockModel(map[string]string{"default": server.URL}, URLSuffix{})
	var chunks []string
	err := driver.ChatStreamlyWithMessages("m", []Message{{Role: "user", Content: "q"}}, bedrockTestAPI(), nil, func(c, r *string) error {
		if c != nil {
			chunks = append(chunks, *c)
		}
		return nil
	})
	if err != nil || !reflect.DeepEqual(chunks, []string{"answer", "[DONE]"}) {
		t.Fatal(chunks, err)
	}
}
func TestBedrockBinaryStreamFailures(t *testing.T) {
	valid := bedrockValidEvents()
	cases := map[string][]bedrockTestEvent{
		"truncated": valid[:3], "no-text": {valid[0], valid[3]}, "invalid-stop": {valid[0], valid[1], valid[2], {event: "messageStop", payload: `{"stopReason":"guardrail_intervened"}`}},
		"missing-index": {valid[0], {event: "contentBlockDelta", payload: `{"delta":{"text":"x"}}`}}, "null-text": {valid[0], {event: "contentBlockDelta", payload: `{"contentBlockIndex":0,"delta":{"text":null}}`}}, "tool": {valid[0], {event: "contentBlockDelta", payload: `{"contentBlockIndex":0,"delta":{"toolUse":{}}}`}},
		"exception": {valid[0], {kind: "exception", event: "throttlingException", payload: `{"message":"private-secret"}`}}, "exception-after-stop": append(append([]bedrockTestEvent{}, valid...), bedrockTestEvent{kind: "exception", event: "modelStreamErrorException", payload: `{}`}), "malformed": {valid[0], {event: "messageStop", payload: `{bad}`}}, "unclosed": {valid[0], valid[1], valid[3]}, "unknown": {valid[0], {event: "anything", payload: `{}`}},
	}
	for name, events := range cases {
		t.Run(name, func(t *testing.T) {
			frames := bedrockTestFrames(t, events)
			done := false
			err := decodeBedrockEventStream(context.Background(), bytes.NewReader(frames), func(c, r *string) error { done = done || c != nil && *c == "[DONE]"; return nil })
			if err == nil || done || strings.Contains(err.Error(), "private-secret") {
				t.Fatal(err, done)
			}
		})
	}
	frames := bedrockTestFrames(t, valid)
	frames[len(frames)-1] ^= 1
	if err := decodeBedrockEventStream(context.Background(), bytes.NewReader(frames), func(*string, *string) error { return nil }); err == nil {
		t.Fatal("CRC corruption accepted")
	}
}
func TestBedrockStreamCancellationIdleAndSender(t *testing.T) {
	for _, mode := range []string{"idle", "cancel", "sender"} {
		t.Run(mode, func(t *testing.T) {
			server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
				if mode != "idle" {
					_, _ = w.Write(bedrockTestFrames(t, bedrockValidEvents()[:2]))
				}
				w.(http.Flusher).Flush()
				<-r.Context().Done()
			}))
			defer server.Close()
			driver := NewBedrockModel(map[string]string{"default": server.URL}, URLSuffix{})
			driver.streamIdleTimeout = 40 * time.Millisecond
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			api := bedrockTestAPI()
			api.Context = ctx
			want := errors.New("sender stopped")
			err := driver.ChatStreamlyWithMessages("m", []Message{{Role: "user", Content: "q"}}, api, nil, func(c, r *string) error {
				if mode == "sender" {
					return want
				}
				cancel()
				return nil
			})
			if mode == "sender" && !errors.Is(err, want) || mode == "cancel" && !errors.Is(err, context.Canceled) || mode == "idle" && !errors.Is(err, context.DeadlineExceeded) {
				t.Fatal(err)
			}
		})
	}
}
func TestBedrockDefaultChainAndSTS(t *testing.T) {
	// These are deterministic non-production credentials; no default-chain network probe is allowed.
	t.Setenv("AWS_ACCESS_KEY_ID", "SOURCEKEY")
	t.Setenv("AWS_SECRET_ACCESS_KEY", "source-secret")
	t.Setenv("AWS_SESSION_TOKEN", "source-token")
	t.Setenv("AWS_EC2_METADATA_DISABLED", "true")
	t.Setenv("AWS_CONFIG_FILE", t.TempDir()+"/absent")
	t.Setenv("AWS_SHARED_CREDENTIALS_FILE", t.TempDir()+"/absent")
	var stsCalls atomic.Int32
	server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		verifyBedrockSignature(t, r, raw, "SOURCEKEY", "source-secret", "source-token", "us-east-1", "sts")
		body := string(raw)
		if !strings.Contains(body, "Action=AssumeRole") || !strings.Contains(body, "ExternalId=external") || !strings.Contains(body, "RoleSessionName=OwnedSession") {
			t.Error(body)
		}
		stsCalls.Add(1)
		w.Header().Set("Content-Type", "text/xml")
		fmt.Fprint(w, `<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><AssumeRoleResult><Credentials><AccessKeyId>TEMPKEY</AccessKeyId><SecretAccessKey>temporary-secret</SecretAccessKey><SessionToken>temporary-token</SessionToken><Expiration>2035-01-01T00:00:00Z</Expiration></Credentials></AssumeRoleResult><ResponseMetadata><RequestId>owned</RequestId></ResponseMetadata></AssumeRoleResponse>`)
	}))
	defer server.Close()
	driver := NewBedrockModel(map[string]string{"sts:us-east-1": server.URL}, URLSuffix{})
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	value, err := driver.resolveCredentials(ctx, &bedrockKey{AuthMode: "assume_role"}, "us-east-1")
	if err != nil || value.AccessKeyID != "SOURCEKEY" {
		t.Fatal(value.Source, err)
	}
	value, err = driver.resolveCredentials(ctx, &bedrockKey{AuthMode: "iam_role", RoleARN: "arn:aws:iam::123456789012:role/Owned", ExternalID: "external", SessionName: "OwnedSession"}, "us-east-1")
	if err != nil || value.AccessKeyID != "TEMPKEY" || value.SessionToken != "temporary-token" || !value.CanExpire || stsCalls.Load() != 1 {
		t.Fatal(value.Source, err, stsCalls.Load())
	}
}
func TestBedrockInstanceIsolation(t *testing.T) {
	var wg sync.WaitGroup
	for _, region := range []string{"us-east-1", "eu-west-1"} {
		key, secret := "KEY"+region, "SECRET"+region
		server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			raw, _ := io.ReadAll(r.Body)
			verifyBedrockSignature(t, r, raw, key, secret, "", region, "bedrock")
			fmt.Fprint(w, `{"modelSummaries":[{"modelId":"`+region+`"}]}`)
		}))
		driver := NewBedrockModel(map[string]string{region: server.URL}, URLSuffix{})
		for i := 0; i < 8; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				raw, _ := json.Marshal(bedrockKey{AuthMode: "access_key_secret", Region: region, AccessKey: key, SecretKey: secret})
				blob := string(raw)
				names, err := driver.ListModels(&APIConfig{APIKey: &blob})
				if err != nil || !reflect.DeepEqual(names, []string{region}) {
					t.Error(names, err)
				}
			}()
		}
	}
	wg.Wait()
}

func TestBedrockTransportFailures(t *testing.T) {
	var forwarded atomic.Int32
	target := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { forwarded.Add(1) }))
	for _, status := range []int{http.StatusForbidden, http.StatusTemporaryRedirect} {
		server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Location", target.URL)
			w.WriteHeader(status)
			fmt.Fprint(w, "private-secret")
		}))
		driver := NewBedrockModel(map[string]string{"default": server.URL}, URLSuffix{})
		if _, err := driver.ListModels(bedrockTestAPI()); err == nil || !strings.Contains(err.Error(), fmt.Sprint(status)) || strings.Contains(err.Error(), "private-secret") {
			t.Fatal(err)
		}
	}
	if forwarded.Load() != 0 {
		t.Fatal("signed credentials followed redirect")
	}
	var calls atomic.Int32
	server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1); fmt.Fprint(w, `{}`) }))
	driver := NewBedrockModel(map[string]string{"default": server.URL}, URLSuffix{})
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	api := bedrockTestAPI()
	api.Context = ctx
	if _, err := driver.ListModels(api); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if calls.Load() != 0 {
		t.Fatal("pre-canceled request reached HTTP")
	}
	done := false
	err := driver.ChatStreamlyWithMessages("m", []Message{{Role: "user", Content: "q"}}, bedrockTestAPI(), nil, func(c, r *string) error { done = done || c != nil && *c == "[DONE]"; return nil })
	if err == nil || done {
		t.Fatal("JSON accepted as binary stream", err, done)
	}
}
func TestBedrockSTSFailureHasNoRuntimeCall(t *testing.T) {
	t.Setenv("AWS_ACCESS_KEY_ID", "SOURCEKEY")
	t.Setenv("AWS_SECRET_ACCESS_KEY", "source-secret")
	t.Setenv("AWS_SESSION_TOKEN", "")
	t.Setenv("AWS_EC2_METADATA_DISABLED", "true")
	t.Setenv("AWS_CONFIG_FILE", t.TempDir()+"/absent")
	t.Setenv("AWS_SHARED_CREDENTIALS_FILE", t.TempDir()+"/absent")
	for _, mode := range []string{"denied", "missing", "deadline"} {
		t.Run(mode, func(t *testing.T) {
			var runtime atomic.Int32
			server := afterf9Server(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/" {
					runtime.Add(1)
					return
				}
				if mode == "deadline" {
					// Drain STS Query input before waiting for client cancellation.
					// net/http otherwise defers disconnect detection on an unread request body.
					_, _ = io.Copy(io.Discard, r.Body)
					<-r.Context().Done()
					return
				}
				if mode == "denied" {
					w.WriteHeader(403)
					fmt.Fprint(w, `<ErrorResponse><Error><Code>AccessDenied</Code><Message>private-secret</Message></Error></ErrorResponse>`)
					return
				}
				fmt.Fprint(w, `<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><AssumeRoleResult/></AssumeRoleResponse>`)
			}))
			driver := NewBedrockModel(map[string]string{"default": server.URL, "sts:us-east-1": server.URL}, URLSuffix{})
			key := `{"auth_mode":"iam_role","bedrock_region":"us-east-1","aws_role_arn":"arn:aws:iam::123456789012:role/Owned"}`
			ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
			defer cancel()
			value, err := driver.ListModels(&APIConfig{APIKey: &key, Context: ctx})
			if err == nil || value != nil || strings.Contains(err.Error(), "private-secret") || runtime.Load() != 0 {
				t.Fatal(value, err, runtime.Load())
			}
			if mode == "deadline" && !errors.Is(err, context.DeadlineExceeded) {
				t.Fatal(err)
			}
		})
	}
}
