package handler_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"github.com/aws/aws-sdk-go-v2/aws/protocol/eventstream"
	"io"
	"multirag/internal/cli"
	"multirag/internal/dao"
	"multirag/internal/testsupport"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// These helpers exercise the canonical consumer chain, not provider facades.
// Provider-specific numeric/stream parsing is additionally mapped to its driver suites.
func exerciseCanonicalChat(t *testing.T, f *canonicalProviderFixture, provider, model, instance string) {
	t.Helper()
	var calls atomic.Int64
	var received atomic.Value
	var mode atomic.Int32
	key := f.Prefix + "-" + provider + "-" + instance
	if (provider == "ModelScope" || provider == "LocalAI") && instance == "one" {
		key = ""
	}
	answer := "answer-" + provider + "-" + instance
	history := []any{map[string]any{"role": "system", "content": "rules"}, map[string]any{"role": "assistant", "content": "previous"}, map[string]any{"role": "user", "content": "question"}}
	configured := dao.GetModelProviderManager().FindProvider(provider)
	if configured == nil {
		t.Fatal("target provider config absent", provider)
	}
	upstream := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		expectedAuth := "Bearer " + key
		if key == "" {
			expectedAuth = ""
		}
		if provider == "CoHere" {
			expectedAuth = "bearer " + key
		}
		if r.Method != "POST" || (provider != "Anthropic" && provider != "Azure-OpenAI" && r.Header.Get("Authorization") != expectedAuth && !(provider == "CoHere" && r.Header.Get("Authorization") == "Bearer "+key)) {
			t.Error("canonical provider method/credential changed")
		}
		if provider == "Azure-OpenAI" && (r.Header.Get("api-key") != key || r.URL.Query().Get("api-version") == "" || !strings.Contains(r.URL.Path, "/deployments/"+model+"/")) {
			t.Error("Azure native deployment/auth/version lost")
		}
		if provider == "Anthropic" && (r.Header.Get("x-api-key") != key || r.Header.Get("anthropic-version") != "2023-06-01") {
			t.Error("native Anthropic credentials lost")
		}
		suffix := strings.Trim(configured.URLSuffix.Chat, "/")
		if provider == "HuaweiCloud" && model == "DeepSeek-V3" {
			suffix = configured.URLSuffix.AsyncChat
		}
		if suffix != "" && !strings.HasSuffix(r.URL.Path, suffix) {
			t.Error("configured chat suffix changed", r.URL.Path)
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		wireModel := model
		if provider == "HuaweiCloud" && model == "DeepSeek-V3" {
			wireModel = "deepseek-v3"
		}
		if provider != "Azure-OpenAI" && body["model"] != wireModel {
			t.Error("registered model name changed")
		}
		if provider == "Anthropic" {
			if calls.Load() == 1 && body["system"] != "rules" {
				t.Error("native system lost")
			}
			received.Store(append([]any{history[0]}, body["messages"].([]any)...))
		} else {
			received.Store(body["messages"])
		}
		if mode.Load() == 1 {
			w.WriteHeader(503)
			fmt.Fprint(w, "private-provider-body")
			return
		}
		if provider == "Anthropic" {
			fmt.Fprintf(w, `{"content":[{"type":"text","text":%q}]}`, answer)
			return
		}
		if provider == "CoHere" {
			if body["stream"] == true {
				fmt.Fprintf(w, "data: {\"type\":\"content-delta\",\"delta\":{\"message\":{\"content\":{\"text\":%q}}}}\n\ndata: {\"type\":\"message-end\"}\n\n", answer)
			} else {
				fmt.Fprintf(w, `{"message":{"content":[{"type":"text","text":%q}]}}`, answer)
			}
			return
		}
		if body["stream"] == true {
			fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":%q}}]}\n\ndata: [DONE]\n\n", answer)
		} else {
			fmt.Fprintf(w, `{"choices":[{"message":{"content":%q}}]}`, answer)
		}
	}))
	id := f.enroll(t, provider, instance, key, upstream.URL, model, "chat")
	if calls.Load() != 0 {
		t.Fatal("enrollment contacted supplier")
	}
	body := map[string]any{"model_id": id, "messages": history, "model_class": "invented-family", "model_type": "embedding"}
	result := f.requireSuccess(t, "owner", "POST", "/api/v1/chat/completions", body)
	if result["answer"] != answer || !reflect.DeepEqual(received.Load(), history) {
		t.Fatal("actual response or message history lost")
	}
	body["stream"] = true
	_, _, raw := f.request(t, "owner", "POST", "/api/v1/chat/completions", body)
	if provider == "Jina" || provider == "Anthropic" {
		if !strings.Contains(raw, "error") || strings.Contains(raw, "[DONE]") || calls.Load() != 1 {
			t.Fatal("unsupported native stream fabricated success")
		}
	} else if !strings.Contains(raw, answer) || strings.Count(raw, "[DONE]") != 1 {
		t.Fatal("stream answer/unique terminal lost")
	}
	delete(body, "stream")
	client := f.cli(t, "owner")
	cmd, err := cli.NewParser(fmt.Sprintf("CHAT WITH '%s' MESSAGE 'question';", id)).Parse(cli.APIMode)
	if err != nil {
		t.Fatal(err)
	}
	response, err := client.ExecuteCommand(cmd)
	if err != nil {
		t.Fatal("canonical CLI consumer", err)
	}
	encoded, err := json.Marshal(response)
	if err != nil || !strings.Contains(string(encoded), answer) {
		t.Fatal("CLI response lost actual answer")
	}
	before := f.snapshot(t)
	beforeCalls := calls.Load()
	for _, actor := range []string{"", "foreign", "inactive", "revoked", "invited", "member", "disabledtenant"} {
		_, denied, _ := f.request(t, actor, "POST", "/api/v1/chat/completions", body)
		if providerEnvelopeError(denied) == nil {
			t.Fatal("denied actor inferred", actor)
		}
	}
	for _, invalid := range []any{map[string]any{"model_id": 123, "messages": history}, map[string]any{"model_id": id, "message": "legacy"}, map[string]any{"model_id": id, "messages": []any{42}}} {
		_, denied, _ := f.request(t, "owner", "POST", "/api/v1/chat/completions", invalid)
		if providerEnvelopeError(denied) == nil {
			t.Fatal("invalid target payload inferred")
		}
	}
	if f.snapshot(t) != before || calls.Load() != beforeCalls {
		t.Fatal("denied calls changed SQL or contacted supplier")
	}
	mode.Store(1)
	_, failed, raw := f.request(t, "owner", "POST", "/api/v1/chat/completions", body)
	if providerEnvelopeError(failed) == nil || strings.Contains(raw, "private-provider-body") {
		t.Fatal("HTTP provider failure accepted or leaked")
	}
	if f.snapshot(t) != before {
		t.Fatal("supplier failure changed SQL")
	}
	mode.Store(0)
	path := "/api/v1/providers/" + url.PathEscape(provider) + "/instances/" + instance + "/models/" + model
	f.requireSuccess(t, "owner", "PATCH", path, map[string]any{"status": "inactive"})
	beforeCalls = calls.Load()
	_, failed, _ = f.request(t, "owner", "POST", "/api/v1/chat/completions", body)
	if providerEnvelopeError(failed) == nil || calls.Load() != beforeCalls {
		t.Fatal("inactive model reached supplier")
	}
	f.requireSuccess(t, "owner", "PATCH", path, map[string]any{"status": "active"})
	var persistedID string
	if err := f.Read.QueryRow("SELECT id FROM tenant_model WHERE id=?", id).Scan(&persistedID); err != nil || persistedID != id {
		t.Fatal("status toggle replaced UUID", err)
	}
}

func exerciseCanonicalInference(t *testing.T, f *canonicalProviderFixture, provider, model, instance, kind string) {
	t.Helper()
	dimension := 2
	if provider == "DeepInfra" && kind == "embedding" {
		dimension = 32
	}
	instance += "-" + kind
	var calls atomic.Int64
	var mode atomic.Int32
	canceled := make(chan struct{}, 1)
	key := f.Prefix + provider + instance + kind
	if provider == "LocalAI" && strings.HasPrefix(instance, "one-") {
		key = ""
	}
	region := "us-east-1"
	if instance == "two-embedding" {
		region = "eu-west-1"
	}
	if provider == "Bedrock" {
		key = fmt.Sprintf(`{"auth_mode":"access_key_secret","bedrock_ak":%q,"bedrock_sk":"private-secret","bedrock_session_token":%q,"bedrock_region":%q}`, "AKID"+instance, "token"+instance, region)
	}
	configured := dao.GetModelProviderManager().FindProvider(provider)
	upstream := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		expectedAuth := "Bearer " + key
		if key == "" {
			expectedAuth = ""
		}
		if r.Method != "POST" || provider != "Azure-OpenAI" && provider != "Bedrock" && r.Header.Get("Authorization") != expectedAuth {
			t.Error("inference instance credential/method changed")
		}
		if provider == "Azure-OpenAI" && (r.Header.Get("api-key") != key || r.URL.Query().Get("api-version") == "" || !strings.Contains(r.URL.Path, "/deployments/"+model+"/")) {
			t.Error("Azure native inference deployment/auth/version lost")
		}
		if provider == "Bedrock" && (!strings.Contains(r.Header.Get("Authorization"), "Credential=AKID"+instance+"/") || !strings.Contains(r.Header.Get("Authorization"), "/"+region+"/bedrock/aws4_request") || r.URL.Path != "/model/"+model+"/invoke") {
			t.Error("native Bedrock selected instance/region/model/signature lost")
		}
		if mode.Load() == 2 {
			w.WriteHeader(503)
			fmt.Fprint(w, "private-provider-body")
			return
		}

		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		if mode.Load() == 3 {
			select {
			case <-r.Context().Done():
				canceled <- struct{}{}
			case <-time.After(3 * time.Second):
			}
			return
		}
		if provider != "Replicate" && provider != "Azure-OpenAI" && provider != "Bedrock" && provider != "HuggingFace" && !(provider == "DeepInfra" && kind == "rerank") && body["model"] != model {
			t.Error("inference model changed")
		}
		if kind == "embedding" {
			suffix := strings.Trim(configured.URLSuffix.Embedding, "/")
			if provider == "HuggingFace" {
				suffix += "/" + model
			}
			if provider != "Replicate" && provider != "Bedrock" && !strings.HasSuffix(r.URL.Path, suffix) {
				t.Error("embedding endpoint changed", r.URL.Path)
			}
			texts, _ := body["input"].([]any)
			if provider == "HuggingFace" {
				texts, _ = body["inputs"].([]any)
			}
			if provider == "Bedrock" {
				texts, _ = body["texts"].([]any)
				if body["input_type"] != "search_document" {
					t.Error("native Bedrock Cohere input_type changed")
				}
			}
			if provider == "CoHere" {
				texts, _ = body["texts"].([]any)
				if body["input_type"] != "search_document" || body["output_dimension"] != float64(dimension) {
					t.Error("native CoHere embedding body changed")
				}
			}
			if provider == "Upstage" {
				if _, exists := body["dimensions"]; exists {
					t.Error("Upstage gained non-native dimensions wire")
				}
			}
			if provider == "ollama" {
				texts, _ = body["input"].([]any)
			}
			if provider == "Replicate" {
				input, _ := body["input"].(map[string]any)
				if batch, ok := input["text_batch"].(string); ok {
					json.Unmarshal([]byte(batch), &texts)
				} else {
					texts = []any{input["text"]}
				}
			}
			if provider == "VolcEngine" {
				input, _ := body["input"].([]any)
				if len(input) != 1 {
					t.Error("native Volc one-text request changed")
					return
				}
				item, _ := input[0].(map[string]any)
				text, _ := item["text"].(string)
				var index int
				if _, err := fmt.Sscanf(text, "text-%d", &index); err != nil || item["type"] != "text" {
					t.Error("native Volc multimodal text lost")
				}
				if mode.Load() == 1 {
					fmt.Fprint(w, `{"data":{"embedding":[null,1]}}`)
					return
				}
				fmt.Fprintf(w, `{"data":{"embedding":[%d,0]}}`, index)
				return
			}
			if len(texts) == 0 {
				t.Error("embedding input missing")
			}
			if mode.Load() == 1 {
				fmt.Fprint(w, `{"data":[{"index":0,"embedding":[null,1]}],"embeddings":[[null,1]],"id":"bad","status":"failed"}`)
				return
			}
			rows := []any{}
			vectors := []any{}
			replicate := []any{}
			for i := len(texts) - 1; i >= 0; i-- {
				vector := make([]float64, dimension)
				vector[0] = float64(i)
				rows = append(rows, map[string]any{"index": i, "embedding": vector})
			}
			for i := range texts {
				vectors = append(vectors, []float64{float64(i), 0})
				replicate = append(replicate, map[string]any{"embedding": []float64{float64(i), 0}})
			}
			switch provider {
			case "CoHere":
				json.NewEncoder(w).Encode(map[string]any{"embeddings": map[string]any{"float": vectors}})
			case "ollama", "Bedrock":
				json.NewEncoder(w).Encode(map[string]any{"embeddings": vectors})
			case "Replicate":
				json.NewEncoder(w).Encode(map[string]any{"id": "prediction", "status": "succeeded", "output": replicate})
			default:
				json.NewEncoder(w).Encode(map[string]any{"data": rows})
			}
		} else {
			query := body["query"]
			if provider == "NVIDIA" {
				query, _ = query.(map[string]any)["text"]
				if len(body["passages"].([]any)) != 2 {
					t.Error("NVIDIA native passages lost")
				}
			}
			if provider == "Replicate" {
				var pairs [][]string
				if json.Unmarshal([]byte(body["input"].(map[string]any)["input_list"].(string)), &pairs) != nil || len(pairs) != 2 {
					t.Error("Replicate native input_list lost")
				} else {
					query = pairs[0][0]
				}
			}
			if query != "query" {
				t.Error("rerank query lost")
			}
			if mode.Load() == 1 {
				fmt.Fprint(w, `{"results":[{"index":null,"relevance_score":null}],"data":[{"index":null,"relevance_score":null}],"scores":[null,0]}`)
				return
			}
			rows := []any{map[string]any{"index": 1, "relevance_score": 0.9}}
			field := "results"
			if provider == "Voyage" || provider == "Gitee" {
				field = "data"
			}
			if provider == "NVIDIA" {
				json.NewEncoder(w).Encode(map[string]any{"rankings": []any{map[string]any{"index": 1, "logit": 0.9}}})
			} else if provider == "Replicate" {
				json.NewEncoder(w).Encode(map[string]any{"id": "prediction", "status": "succeeded", "output": []float64{0, 0.9}})
			} else if provider == "DeepInfra" {
				json.NewEncoder(w).Encode(map[string]any{"scores": []float64{0, 0.9}})
			} else {
				json.NewEncoder(w).Encode(map[string]any{field: rows})
			}
		}
	}))
	id := f.enroll(t, provider, instance, key, upstream.URL, model, kind)
	size := 33
	if provider == "VolcEngine" {
		size = 2
	}
	if provider == "SiliconFlow" {
		size = 32
	}
	texts := make([]string, size)
	for i := range texts {
		texts[i] = fmt.Sprintf("text-%d", i)
	}
	path := "/api/v1/embeddings"
	body := map[string]any{"model_id": id, "texts": texts, "dimension": dimension}
	syntax := fmt.Sprintf("EMBED TEXT 'text-0' 'text-1' WITH '%s' DIMENSION %d;", id, dimension)
	if kind == "rerank" {
		path = "/api/v1/rerank"
		body = map[string]any{"model_id": id, "query": "query", "documents": []string{"a", "b"}, "top_n": 1}
		syntax = fmt.Sprintf("RERANK QUERY 'query' DOCUMENT 'a' 'b' WITH '%s' TOP 1;", id)
	}
	before := f.snapshot(t)
	result := f.requireSuccess(t, "owner", "POST", path, body)
	rows, ok := result["data"].([]any)
	if !ok {
		t.Fatal("typed inference data missing")
	}
	if kind == "embedding" {
		expectedCalls := int64(1)
		if provider == "VolcEngine" {
			expectedCalls = int64(size)
		}
		if len(rows) != size || calls.Load() != expectedCalls {
			t.Fatal("target single request/count changed", len(rows), calls.Load())
		}
		seen := map[int]bool{}
		for _, raw := range rows {
			r := raw.(map[string]any)
			i := int(r["index"].(float64))
			v := r["embedding"].([]any)
			if i < 0 || i >= size || seen[i] || len(v) != dimension || v[0] != float64(i) || v[1] != float64(0) {
				t.Fatal("typed index/zero identity lost")
			}
			seen[i] = true
		}
	} else if len(rows) != 1 || rows[0].(map[string]any)["index"] != float64(1) || rows[0].(map[string]any)["relevance_score"] != 0.9 {
		t.Fatal("rerank TopN/index/score lost", rows)
	}
	if provider == "SiliconFlow" && kind == "embedding" {
		beforeCount := calls.Load()
		body["texts"] = append(texts, "overflow")
		_, overflow, _ := f.request(t, "owner", "POST", path, body)
		if providerEnvelopeError(overflow) == nil || calls.Load() != beforeCount {
			t.Fatal("target maximum32 boundary ignored")
		}
		body["texts"] = texts
	}
	command, err := cli.NewParser(syntax).Parse(cli.APIMode)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = f.cli(t, "owner").ExecuteCommand(command); err != nil {
		t.Fatal("actual inference CLI", err)
	}
	beforeCalls := calls.Load()
	for _, who := range []string{"", "foreign", "inactive", "revoked", "invited", "member", "disabledtenant"} {
		_, failed, _ := f.request(t, who, "POST", path, body)
		if providerEnvelopeError(failed) == nil {
			t.Fatal("unauthorized inference")
		}
	}
	if calls.Load() != beforeCalls || f.snapshot(t) != before {
		t.Fatal("denied inference side effects")
	}
	for _, m := range []int32{1, 2} {
		mode.Store(m)
		_, failed, raw := f.request(t, "owner", "POST", path, body)
		if providerEnvelopeError(failed) == nil || strings.Contains(raw, "private-provider-body") {
			t.Fatal("malformed/provider failure accepted or leaked", provider, kind, "mode", m, "code", failed["code"], "leak", strings.Contains(raw, "private-provider-body"))
		}
	}
	if f.snapshot(t) != before {
		t.Fatal("failure changed SQL")
	}
	mode.Store(0)
	if err := dao.DB.Exec("UPDATE tenant_model_instance SET status='inactive' WHERE id=(SELECT instance_id FROM tenant_model WHERE id=?)", id).Error; err != nil {
		t.Fatal(err)
	}
	beforeCalls = calls.Load()
	_, failed, _ := f.request(t, "owner", "POST", path, body)
	if providerEnvelopeError(failed) == nil || calls.Load() != beforeCalls {
		t.Fatal("disabled instance reached inference")
	}
	if err := dao.DB.Exec("UPDATE tenant_model_instance SET status='active' WHERE id=(SELECT instance_id FROM tenant_model WHERE id=?)", id).Error; err != nil {
		t.Fatal(err)
	}
	// Incoming cancellation must interrupt a real supplier request.
	mode.Store(3)
	raw, _ := json.Marshal(body)
	ctx, cancel := context.WithTimeout(t.Context(), 200*time.Millisecond)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, "POST", f.url+path, bytes.NewReader(raw))
	req.Header.Set("Authorization", "Bearer "+f.tokens["owner"])
	req.Header.Set("Content-Type", "application/json")
	response, err := f.client.Do(req)
	if response != nil {
		io.Copy(io.Discard, response.Body)
		response.Body.Close()
	}
	if err == nil || ctx.Err() == nil {
		t.Fatal("incoming cancellation lost")
	}
	select {
	case <-canceled:
	case <-time.After(2 * time.Second):
		t.Fatal("incoming cancellation did not reach supplier")
	}
}

// Referenced by media helper below, and deliberately uses JSON bytes instead of a private file wrapper.
var _ = base64.StdEncoding

func exerciseCanonicalNativeChat(t *testing.T, f *canonicalProviderFixture, provider, model, instance string) {
	t.Helper()
	var calls atomic.Int64
	var mode atomic.Int32
	key := f.Prefix + provider + instance
	region := "us-east-1"
	if instance == "two" {
		region = "eu-west-1"
	}
	if provider == "Bedrock" {
		key = fmt.Sprintf(`{"auth_mode":"access_key_secret","bedrock_ak":%q,"bedrock_sk":"private-secret","bedrock_session_token":%q,"bedrock_region":%q}`, "AKID"+instance, "token"+instance, region)
	}
	answer := "answer-" + provider + "-" + instance
	upstream := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Method != "POST" {
			t.Error("native chat method")
		}
		switch provider {
		case "Google":
			if r.Header.Get("X-Goog-Api-Key") != key {
				t.Error("Google instance key lost")
			}
		case "Bedrock":
			if !strings.Contains(r.Header.Get("Authorization"), "Credential=AKID"+instance+"/") || !strings.Contains(r.Header.Get("Authorization"), "/"+region+"/bedrock/aws4_request") {
				t.Error("Bedrock native SigV4 instance/region lost")
			}
		case "ollama":
			if r.Header.Get("Authorization") != "" {
				t.Error("native local authorization changed")
			}
		default:
			if r.Header.Get("Authorization") != "Bearer "+key {
				t.Error("native provider credential lost")
			}
		}
		var body map[string]any
		if json.NewDecoder(r.Body).Decode(&body) != nil {
			t.Error("native body")
			return
		}
		if mode.Load() == 1 {
			w.WriteHeader(503)
			fmt.Fprint(w, "private-provider-body")
			return
		}
		if mode.Load() == 2 {
			fmt.Fprint(w, `{}`)
			return
		}
		stream := body["stream"] == true
		switch provider {
		case "Google":
			if !strings.Contains(r.URL.Path, "models/"+model+":") {
				t.Error("Google bound model path lost")
			}
			content, _ := body["contents"].([]any)
			if len(content) == 0 {
				t.Error("Google contents missing")
			}
			last := content[len(content)-1].(map[string]any)
			parts := last["parts"].([]any)
			if parts[0].(map[string]any)["text"] != "question" {
				t.Error("Google prompt lost")
			}
			payload := fmt.Sprintf(`{"candidates":[{"finishReason":"STOP","content":{"role":"model","parts":[{"text":%q}]}}]}`, answer)
			if strings.Contains(r.URL.Path, ":streamGenerateContent") {
				w.Header().Set("Content-Type", "text/event-stream")
				fmt.Fprintf(w, "data: %s\n\n", payload)
			} else {
				fmt.Fprint(w, payload)
			}
		case "Bedrock":
			messages, _ := body["messages"].([]any)
			if len(messages) != 1 || messages[0].(map[string]any)["content"].([]any)[0].(map[string]any)["text"] != "question" {
				t.Error("Bedrock message mapping lost")
			}
			if !strings.Contains(r.URL.Path, model) {
				t.Error("Bedrock model path lost")
			}
			if strings.HasSuffix(r.URL.Path, "converse-stream") {
				w.Header().Set("Content-Type", "application/vnd.amazon.eventstream")
				events := [][2]string{{"messageStart", `{"role":"assistant"}`}, {"contentBlockDelta", fmt.Sprintf(`{"contentBlockIndex":0,"delta":{"text":%q}}`, answer)}, {"contentBlockStop", `{"contentBlockIndex":0}`}, {"messageStop", `{"stopReason":"end_turn"}`}}
				for _, e := range events {
					if err := eventstream.NewEncoder().Encode(w, eventstream.Message{Headers: eventstream.Headers{{Name: ":message-type", Value: eventstream.StringValue("event")}, {Name: ":event-type", Value: eventstream.StringValue(e[0])}}, Payload: []byte(e[1])}); err != nil {
						t.Error(err)
					}
				}
			} else {
				fmt.Fprintf(w, `{"output":{"message":{"role":"assistant","content":[{"text":%q}]}},"stopReason":"end_turn"}`, answer)
			}
		case "Replicate":
			if !strings.HasSuffix(r.URL.Path, "/models/"+model+"/predictions") {
				t.Error("Replicate official model path lost", r.URL.Path)
			}
			input, _ := body["input"].(map[string]any)
			if !strings.Contains(fmt.Sprint(input["prompt"]), "question") {
				t.Error("Replicate prompt lost")
			}
			fmt.Fprintf(w, `{"id":"prediction","status":"succeeded","output":%q}`, answer)
		case "ollama":
			if body["model"] != model || r.URL.Path != "/api/chat" {
				t.Error("Ollama model/endpoint lost")
			}
			messages := body["messages"].([]any)
			if messages[len(messages)-1].(map[string]any)["content"] != "question" {
				t.Error("Ollama prompt lost")
			}
			if stream {
				w.Header().Set("Content-Type", "application/x-ndjson")
			}
			fmt.Fprintf(w, `{"message":{"role":"assistant","content":%q},"done":true}`, answer)
		}
	}))
	id := f.enroll(t, provider, instance, key, upstream.URL, model, "chat")
	body := map[string]any{"model_id": id, "messages": []any{map[string]any{"role": "user", "content": "question"}}}
	before := f.snapshot(t)
	result := f.requireSuccess(t, "owner", "POST", "/api/v1/chat/completions", body)
	if result["answer"] != answer {
		t.Fatal("native answer lost")
	}
	body["stream"] = true
	_, _, raw := f.request(t, "owner", "POST", "/api/v1/chat/completions", body)
	if !strings.Contains(raw, answer) || strings.Count(raw, "[DONE]") != 1 {
		t.Fatal("native stream answer/terminal lost")
	}
	delete(body, "stream")
	command, err := cli.NewParser(fmt.Sprintf("CHAT WITH '%s' MESSAGE 'question';", id)).Parse(cli.APIMode)
	if err != nil {
		t.Fatal(err)
	}
	resultCLI, err := f.cli(t, "owner").ExecuteCommand(command)
	if err != nil {
		t.Fatal(err)
	}
	rawCLI, _ := json.Marshal(resultCLI)
	if !strings.Contains(string(rawCLI), answer) {
		t.Fatal("native CLI answer lost")
	}
	count := calls.Load()
	for _, who := range []string{"", "foreign", "inactive", "revoked", "invited", "member", "disabledtenant"} {
		_, denied, _ := f.request(t, who, "POST", "/api/v1/chat/completions", body)
		if providerEnvelopeError(denied) == nil {
			t.Fatal("native unauthorized inference")
		}
	}
	if calls.Load() != count || f.snapshot(t) != before {
		t.Fatal("native denied side effects")
	}
	for _, m := range []int32{1, 2} {
		mode.Store(m)
		_, failed, raw := f.request(t, "owner", "POST", "/api/v1/chat/completions", body)
		if providerEnvelopeError(failed) == nil || strings.Contains(raw, "private-provider-body") {
			t.Fatal("native failure accepted or leaked")
		}
	}
	if f.snapshot(t) != before {
		t.Fatal("native failure SQL effects")
	}
}

func exerciseCanonicalMedia(t *testing.T, f *canonicalProviderFixture, provider, model, instance, kind string) {
	t.Helper()
	instance += "-" + kind
	key := f.Prefix + provider + instance
	if provider == "PaddleOCR" {
		key = ""
	}
	var calls atomic.Int64
	var mode atomic.Int32
	config := dao.GetModelProviderManager().FindProvider(provider)
	if config == nil {
		t.Fatal("canonical provider absent", provider)
	}
	audio := []byte{0, 255, 1, 128, 9}
	text := "recognized-" + provider + "-" + instance
	if kind == "ocr" && (provider == "Baidu" || provider == "PaddleOCR" || provider == "Mistral") {
		text += "\n\npage two " + provider + "-" + instance
	}
	wav := make([]byte, 46)
	copy(wav, "RIFF")
	binary.LittleEndian.PutUint32(wav[4:], 38)
	copy(wav[8:], "WAVEfmt ")
	binary.LittleEndian.PutUint32(wav[16:], 16)
	binary.LittleEndian.PutUint16(wav[20:], 1)
	binary.LittleEndian.PutUint16(wav[22:], 1)
	binary.LittleEndian.PutUint32(wav[24:], 16000)
	binary.LittleEndian.PutUint32(wav[28:], 32000)
	binary.LittleEndian.PutUint16(wav[32:], 2)
	binary.LittleEndian.PutUint16(wav[34:], 16)
	copy(wav[36:], "data")
	binary.LittleEndian.PutUint32(wav[40:], 2)
	wav[44] = 1
	wav[45] = 128
	upstream := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if kind == "parse" && r.Method == "GET" {
			if r.Header.Get("Authorization") != "Bearer "+key || r.ContentLength > 0 || !strings.HasSuffix(r.URL.Path, "/owned-task") {
				t.Error("native task query auth/path/body changed")
			}
			state := "done"
			if mode.Load() == 4 {
				state = "running"
			}
			fmt.Fprintf(w, `{"code":0,"data":{"task_id":"owned-task","state":%q,"full_zip_url":"https://example.invalid/result.zip"}}`, state)
			return
		}
		if r.Method != "POST" || (provider != "PaddleOCR" && r.Header.Get("Authorization") != "Bearer "+key) {
			t.Error("media method/credential crossed instance")
		}
		if mode.Load() == 1 {
			w.WriteHeader(503)
			fmt.Fprint(w, "private-provider-body")
			return
		}
		if mode.Load() == 2 {
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, `{}`)
			return
		}
		var body map[string]any
		if strings.Contains(r.Header.Get("Content-Type"), "multipart/form-data") {
			if err := r.ParseMultipartForm(1 << 20); err != nil {
				t.Error(err)
				return
			}
			defer r.MultipartForm.RemoveAll()
			field := "file"
			if provider == "FishAudio" {
				field = "audio"
			}
			if kind == "ocr" && provider == "Gitee" {
				field = "image"
			}
			file, _, err := r.FormFile(field)
			if err != nil {
				t.Error("native file field absent", field)
				return
			}
			raw, _ := io.ReadAll(file)
			file.Close()
			expected := wav
			if kind == "ocr" {
				expected = []byte("owned image bytes")
			}
			if !bytes.Equal(raw, expected) {
				t.Error("actual media bytes changed")
			}
			if provider != "FishAudio" && r.FormValue("model") != model {
				t.Error("media multipart bound model changed")
			}
		} else {
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Error(err)
				return
			}
			if provider == "PaddleOCR" && (r.Header.Get("Authorization") != "" || body["file"] != base64.StdEncoding.EncodeToString([]byte("owned image bytes"))) {
				t.Error("local PaddleOCR keyless bytes changed")
			}
			if provider != "PaddleOCR" && !((provider == "FishAudio" || provider == "xAI") && kind == "tts") && kind != "parse" && body["model"] != model {
				t.Error("media bound model changed")
			}
		}
		switch kind {
		case "speech2text":
			if provider == "OpenRouter" {
				if body["input_audio"].(map[string]any)["data"] != base64.StdEncoding.EncodeToString(wav) {
					t.Error("native base64 audio changed")
				}
			}
			fmt.Fprintf(w, `{"text":%q}`, text)
		case "tts":
			input := body["input"]
			if provider == "MiniMax" || provider == "FishAudio" || provider == "xAI" {
				input = body["text"]
			}
			if provider == "xAI" && (body["voice_id"] != model || body["output_format"].(map[string]any)["codec"] != "wav") {
				t.Error("native xAI voice/codec lost")
			}
			if input != "speak" {
				t.Error("TTS input lost")
			}
			if provider == "FishAudio" && r.Header.Get("model") != model {
				t.Error("native FishAudio model header lost")
			}
			if provider == "MiniMax" {
				fmt.Fprintf(w, `{"base_resp":{"status_code":0},"data":{"audio":%q,"status":2}}`, hex.EncodeToString(audio))
			} else {
				w.Header().Set("Content-Type", "audio/wav")
				w.Write(audio)
			}
		case "ocr":
			switch provider {
			case "ZHIPU-AI":
				fmt.Fprintf(w, `{"md_results":%q}`, text)
			case "Baidu", "PaddleOCR":
				pages := strings.Split(text, "\n\n")
				fmt.Fprintf(w, `{"errorCode":0,"result":{"layoutParsingResults":[{"markdown":{"text":%q}},{"markdown":{"text":%q}}]}}`, pages[0], pages[1])
			case "Mistral":
				pages := strings.Split(text, "\n\n")
				fmt.Fprintf(w, `{"pages":[{"index":1,"markdown":%q},{"index":0,"markdown":%q}]}`, pages[1], pages[0])
			case "302.AI":
				fmt.Fprintf(w, `{"pages":[{"index":0,"markdown":%q}]}`, text)
			case "Gitee":
				fmt.Fprintf(w, `{"text_result":%q}`, text)
			default:
				fmt.Fprintf(w, `{"text":%q}`, text)
			}
		case "parse":
			if body["url"] != "https://example.invalid/owned.pdf" || body["model_version"] != model {
				t.Error("native task submission lost")
			}
			fmt.Fprint(w, `{"code":0,"data":{"task_id":"owned-task"}}`)
		}
	}))
	canonicalKind := kind
	if kind == "speech2text" {
		canonicalKind = "asr"
	}
	if kind == "parse" {
		canonicalKind = "doc_parse"
	}
	id := f.enroll(t, provider, instance, key, upstream.URL, model, canonicalKind)
	path := "/api/v1/audio/transcriptions"
	body := map[string]any{"model_id": id, "file": base64.StdEncoding.EncodeToString(wav), "asr_config": map[string]any{"params": map[string]any{"response_format": "json"}}}
	switch kind {
	case "tts":
		path = "/api/v1/audio/speech"
		body = map[string]any{"model_id": id, "text": "speak", "tts_config": map[string]any{"format": "wav", "params": map[string]any{"voice": "fixture"}}}
	case "ocr":
		path = "/api/v1/file/ocr"
		body = map[string]any{"model_id": id, "content": []byte("owned image bytes")}
	case "parse":
		path = "/api/v1/file/parse"
		body = map[string]any{"model_id": id, "url": "https://example.invalid/owned.pdf"}
	}
	before := f.snapshot(t)
	result := f.requireSuccess(t, "owner", "POST", path, body)
	data, ok := result["data"].(map[string]any)
	if !ok {
		t.Fatal("canonical typed media response lost")
	}
	switch kind {
	case "tts":
		v, err := base64.StdEncoding.DecodeString(data["audio"].(string))
		if err != nil || !bytes.Equal(v, audio) {
			t.Fatal("binary audio changed")
		}
	case "parse":
		if data["task_id"] != "owned-task" || len(data) != 1 {
			t.Fatal("async submission fabricated parsed output")
		}
	default:
		if data["text"] != text {
			t.Fatal("actual media text lost", data)
		}
	}

	if kind == "parse" {
		route := "/api/v1/providers/" + url.PathEscape(provider) + "/instances/" + instance + "/tasks/owned-task"
		task := f.requireSuccess(t, "owner", "GET", route, nil)["data"].(map[string]any)
		segments := task["segments"].([]any)
		if len(segments) != 1 || segments[0].(map[string]any)["index"] != float64(0) || segments[0].(map[string]any)["content"] != "https://example.invalid/result.zip" {
			t.Fatal("task artifact URL changed")
		}
		mode.Store(4)
		_, running, _ := f.request(t, "owner", "GET", route, nil)
		if providerEnvelopeError(running) == nil {
			t.Fatal("unfinished task fabricated parsed document")
		}
		mode.Store(0)
		cmd, err := cli.NewParser(fmt.Sprintf("PARSE WITH '%s@%s@%s' URL 'https://example.invalid/owned.pdf';", model, instance, provider)).Parse(cli.APIMode)
		if err != nil {
			t.Fatal(err)
		}
		response, err := f.cli(t, "owner").ExecuteCommand(cmd)
		if err != nil {
			t.Fatal("actual hosted parse CLI", err)
		}
		raw, _ := json.Marshal(response)
		if !strings.Contains(string(raw), "owned-task") || strings.Contains(string(raw), "segments") {
			t.Fatal("CLI hosted submission fabricated document")
		}
		count := calls.Load()
		_, denied, _ := f.request(t, "foreign", "GET", route, nil)
		if providerEnvelopeError(denied) == nil || calls.Load() != count {
			t.Fatal("foreign task query reached supplier")
		}
	}
	// Actual CLI file consumers use a pre-registered directory and private inputs.
	if kind != "parse" {
		identity := sha256.Sum256([]byte(provider + instance))
		directory := filepath.Join(os.TempDir(), f.Prefix+fmt.Sprintf("-%x", identity[:6]))
		f.Journal.Event("reserve-directory", map[string]any{"path": directory})
		if err := os.Mkdir(directory, 0700); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			if err := os.RemoveAll(directory); err != nil {
				t.Error(err)
			}
			_, err := os.Stat(directory)
			absent := os.IsNotExist(err)
			f.Journal.Event("cleanup-directory", map[string]any{"path": directory, "absent": absent})
			if !absent {
				t.Error("private media artifacts remain")
			}
		})
		player := filepath.Join(directory, "aplay")
		f.Journal.Event("reserve-file", map[string]any{"path": player})
		if err := os.WriteFile(player, []byte("#!/bin/sh\nexit 67\n"), 0700); err != nil {
			t.Fatal(err)
		}
		t.Setenv("PATH", directory+string(os.PathListSeparator)+os.Getenv("PATH"))
		syntax := ""
		if kind == "tts" {
			syntax = fmt.Sprintf("TTS WITH '%s' TEXT 'speak' FORMAT 'wav' SAVE '%s' PARAM '{\"voice\":\"fixture\"}';", id, directory)
		} else {
			extension := ".png"
			input := []byte("owned image bytes")
			if kind == "speech2text" {
				extension = ".wav"
				input = wav
			}
			file := filepath.Join(directory, "input"+extension)
			f.Journal.Event("reserve-file", map[string]any{"path": file})
			if err := os.WriteFile(file, input, 0600); err != nil {
				t.Fatal(err)
			}
			if kind == "speech2text" {
				syntax = fmt.Sprintf("ASR WITH '%s' AUDIO '%s' PARAM '{\"response_format\":\"json\"}';", id, file)
			} else {
				syntax = fmt.Sprintf("OCR WITH '%s' FILE '%s';", id, file)
			}
		}
		if kind == "tts" {
			f.Journal.Event("reserve-file", map[string]any{"path": filepath.Join(directory, "_output.wav")})
		}
		command, err := cli.NewParser(syntax).Parse(cli.APIMode)
		if err != nil {
			t.Fatal("canonical media CLI syntax", err)
		}
		response, err := f.cli(t, "owner").ExecuteCommand(command)
		if err != nil {
			t.Fatal("canonical media CLI actual HTTP", err)
		}
		if kind == "tts" {
			file := filepath.Join(directory, "_output.wav")
			content, err := os.ReadFile(file)
			info, statErr := os.Stat(file)
			if err != nil || statErr != nil || !bytes.Equal(content, audio) || info.Mode().Perm() != 0600 {
				t.Fatal("saved audio bytes/private permissions changed")
			}
			if _, err = f.cli(t, "owner").ExecuteCommand(command); err == nil {
				t.Fatal("CLI overwrote existing output")
			}
			after, _ := os.ReadFile(file)
			if !bytes.Equal(after, audio) {
				t.Fatal("CLI changed existing output")
			}
		} else {
			raw, _ := json.Marshal(response)
			expected, err := json.Marshal(text)
			if err != nil || !strings.Contains(string(raw), string(expected)) {
				t.Fatal("CLI media text changed")
			}
		}
	}
	count := calls.Load()
	for _, who := range []string{"", "foreign", "inactive", "revoked", "invited", "member", "disabledtenant"} {
		_, failed, _ := f.request(t, who, "POST", path, body)
		if providerEnvelopeError(failed) == nil {
			t.Fatal("media unauthorized inference", who)
		}
	}
	if calls.Load() != count || f.snapshot(t) != before {
		t.Fatal("media denied side effects")
	}
	mode.Store(1)
	_, failed, raw := f.request(t, "owner", "POST", path, body)
	if providerEnvelopeError(failed) == nil || strings.Contains(raw, "private-provider-body") {
		t.Fatal("media HTTP failure accepted or leaked")
	}
	// Raw audio is not JSON, so a JSON '{}' error body must not be interpreted as audio.
	mode.Store(2)
	_, failed, _ = f.request(t, "owner", "POST", path, body)
	if providerEnvelopeError(failed) == nil {
		t.Fatal("missing media data accepted", provider, kind)
	}
	if f.snapshot(t) != before {
		t.Fatal("media failures wrote SQL")
	}
}

func exerciseCanonicalLocalParser(t *testing.T, f *canonicalProviderFixture, instance string) {
	t.Helper()
	var calls atomic.Int64
	var mode atomic.Int32
	input := []byte("%PDF-owned local document")
	config := dao.GetModelProviderManager().FindProvider("MinerU")
	if config == nil || config.Class != "local" {
		t.Fatal("local MinerU config absent")
	}
	upstream := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("Authorization") != "" {
			t.Error("keyless local parser gained auth")
		}
		if mode.Load() == 1 {
			w.WriteHeader(503)
			fmt.Fprint(w, "private-provider-body")
			return
		}
		if mode.Load() == 2 {
			fmt.Fprint(w, `{}`)
			return
		}
		if r.Method == "GET" {
			if r.URL.Path != "/tasks/local-task/result" || r.ContentLength > 0 {
				t.Error("native local task path/body changed")
			}
			if mode.Load() == 3 {
				w.WriteHeader(202)
				fmt.Fprint(w, `{"task_id":"local-task","status":"pending"}`)
				return
			}
			fmt.Fprint(w, `{"task_id":"local-task","status":"completed","results":{"b.pdf":{"md_content":"second"},"a.pdf":{"md_content":"first"}}}`)
			return
		}
		if r.Method != "POST" || r.URL.Path != "/file_parse" {
			t.Error("native local parser endpoint changed")
		}
		if err := r.ParseMultipartForm(1 << 20); err != nil {
			t.Error(err)
			return
		}
		defer r.MultipartForm.RemoveAll()
		file, header, err := r.FormFile("files")
		if err != nil {
			t.Error("native files field absent")
			return
		}
		raw, _ := io.ReadAll(file)
		file.Close()
		if !bytes.Equal(raw, input) || header.Filename != "upload_document.pdf" || r.FormValue("backend") != "pipeline" {
			t.Error("native local file/backend changed")
		}
		fmt.Fprint(w, `{"task_id":"local-task","status":"pending"}`)
	}))
	id := f.enroll(t, "MinerU", instance, "", upstream.URL, "pipeline", "doc_parse")
	before := f.snapshot(t)
	body := map[string]any{"model_id": id, "content": input}
	result := f.requireSuccess(t, "owner", "POST", "/api/v1/file/parse", body)["data"].(map[string]any)
	if result["task_id"] != "local-task" || len(result) != 1 {
		t.Fatal("async local submission fabricated document")
	}
	path := "/api/v1/providers/MinerU/instances/" + instance + "/tasks/local-task"
	checkTask := func(result map[string]any) {
		segments := result["data"].(map[string]any)["segments"].([]any)
		if len(segments) != 2 || segments[0].(map[string]any)["content"] != "first" || segments[1].(map[string]any)["content"] != "second" || segments[0].(map[string]any)["index"] != float64(1) {
			t.Fatal("native multi-document task result lost/order changed")
		}
	}
	checkTask(f.requireSuccess(t, "owner", "GET", path, nil))
	identity := sha256.Sum256([]byte(instance))
	directory := filepath.Join(os.TempDir(), f.Prefix+fmt.Sprintf("-local-parse-%x", identity[:6]))
	f.Journal.Event("reserve-directory", map[string]any{"path": directory})
	if err := os.Mkdir(directory, 0700); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(directory); err != nil {
			t.Error(err)
		}
		_, err := os.Stat(directory)
		absent := os.IsNotExist(err)
		f.Journal.Event("cleanup-directory", map[string]any{"path": directory, "absent": absent})
		if !absent {
			t.Error("local parse artifact remains")
		}
	})
	file := filepath.Join(directory, "owned.pdf")
	f.Journal.Event("reserve-file", map[string]any{"path": file})
	if err := os.WriteFile(file, input, 0600); err != nil {
		t.Fatal(err)
	}
	syntax := fmt.Sprintf("PARSE WITH 'pipeline@%s@MinerU' FILE '%s';", instance, file)
	command, err := cli.NewParser(syntax).Parse(cli.APIMode)
	if err != nil {
		t.Fatal(err)
	}
	response, err := f.cli(t, "owner").ExecuteCommand(command)
	if err != nil {
		t.Fatal("actual local parse CLI", err)
	}
	raw, _ := json.Marshal(response)
	if !strings.Contains(string(raw), "local-task") || strings.Contains(string(raw), "segments") {
		t.Fatal("CLI submission fabricated parsed document")
	}
	taskCommand, err := cli.NewParser(fmt.Sprintf("SHOW '%s@MinerU' TASK 'local-task';", instance)).Parse(cli.APIMode)
	if err != nil {
		t.Fatal(err)
	}
	response, err = f.cli(t, "owner").ExecuteCommand(taskCommand)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ = json.Marshal(response)
	if !strings.Contains(string(raw), "first") || !strings.Contains(string(raw), "second") {
		t.Fatal("CLI local task lost document")
	}
	count := calls.Load()
	for _, who := range []string{"", "foreign", "inactive", "revoked", "invited", "member", "disabledtenant"} {
		for _, route := range []struct {
			method, path string
			body         any
		}{{"POST", "/api/v1/file/parse", body}, {"GET", path, nil}} {
			_, failed, _ := f.request(t, who, route.method, route.path, route.body)
			if providerEnvelopeError(failed) == nil {
				t.Fatal("denied local parse/task succeeded", who)
			}
		}
		if _, err := f.cli(t, who).ExecuteCommand(command); err == nil {
			t.Fatal("denied CLI local parse succeeded", who)
		}
	}
	if count != calls.Load() || f.snapshot(t) != before {
		t.Fatal("denied keyless parse changed SQL/contacted supplier")
	}
	for _, value := range []int32{1, 2} {
		mode.Store(value)
		_, failed, raw := f.request(t, "owner", "POST", "/api/v1/file/parse", body)
		if providerEnvelopeError(failed) == nil || strings.Contains(raw, "private-provider-body") {
			t.Fatal("failed local submission succeeded/leaked")
		}
	}
	mode.Store(3)
	_, failed, _ := f.request(t, "owner", "GET", path, nil)
	if providerEnvelopeError(failed) == nil {
		t.Fatal("pending task fabricated document")
	}
	mode.Store(0)
	count = calls.Load()
	body["url"] = "https://example.invalid/file.pdf"
	_, failed, _ = f.request(t, "owner", "POST", "/api/v1/file/parse", body)
	if providerEnvelopeError(failed) == nil || calls.Load() != count {
		t.Fatal("local parser fetched external URL")
	}
	for _, route := range []string{"/api/v1/providers/MinerU/instances/" + instance + "/models?supported=true", "/api/v1/providers/MinerU/instances/" + instance + "/connection"} {
		_, failed, _ = f.request(t, "owner", "GET", route, nil)
		if providerEnvelopeError(failed) == nil || calls.Load() != count {
			t.Fatal("unsupported local catalog fabricated success")
		}
	}
	if f.snapshot(t) != before {
		t.Fatal("failed local parsing wrote SQL")
	}
	// The frozen local driver requires an explicit endpoint, even with no key.
	if err := dao.DB.Exec("UPDATE tenant_model_instance SET extra=? WHERE id=(SELECT instance_id FROM tenant_model WHERE id=?)", `{"region":"default","base_url":""}`, id).Error; err != nil {
		t.Fatal(err)
	}
	missingEndpointBefore := f.snapshot(t)
	delete(body, "url")
	count = calls.Load()
	_, failed, _ = f.request(t, "owner", "POST", "/api/v1/file/parse", body)
	if providerEnvelopeError(failed) == nil {
		t.Fatal("missing local endpoint accepted")
	}
	if _, err := f.cli(t, "owner").ExecuteCommand(command); err == nil {
		t.Fatal("CLI accepted missing local endpoint")
	}
	if calls.Load() != count || f.snapshot(t) != missingEndpointBefore {
		t.Fatal("missing endpoint denial touched supplier/SQL")
	}
}
