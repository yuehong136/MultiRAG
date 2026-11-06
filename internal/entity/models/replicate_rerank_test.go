package models

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
)

func TestReplicateExplicitRerankFactoryAndPreflight(t *testing.T) {
	const version = "7f7c6e9d18336e2cbf07d88e9362d881d2fe4d6a9854ec1260f115cabc106a8c"
	name := "yxzwayne/bge-reranker-v2-m3:" + version
	var calls atomic.Int32
	server := providerTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var body struct {
			Version string `json:"version"`
			Input   struct {
				Pairs string `json:"input_list"`
			} `json:"input"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if r.Method != http.MethodPost || r.URL.Path != "/v1/predictions" || r.Header.Get("Authorization") != "Bearer key" || body.Version != version || body.Input.Pairs != `[["q","d"]]` {
			t.Error("explicit rerank prediction contract", r.Method, r.URL.Path, body)
		}
		fmt.Fprint(w, `{"id":"pred-1","status":"succeeded","output":[0]}`)
	}))
	factory := NewModelFactory()
	driver, err := factory.CreateModelDriver("Replicate", map[string]string{"default": server.URL}, URLSuffix{Chat: "v1/predictions", Models: "v1/models"})
	if err != nil {
		t.Fatal(err)
	}
	result, err := driver.Rerank(&name, "q", []string{"d"}, &APIConfig{APIKey: stringPtr("key")}, nil)
	if err != nil || result == nil || len(result.Data) != 1 || result.Data[0].Index != 0 || result.Data[0].RelevanceScore != 0 || calls.Load() != 1 {
		t.Fatal("explicit rerank driver lost", result, err, calls.Load())
	}
	for _, api := range []*APIConfig{nil, {}, {APIKey: stringPtr("")}, {APIKey: stringPtr(" ")}} {
		if result, err := driver.Rerank(&name, "q", []string{"d"}, api, nil); err == nil || result != nil {
			t.Fatal("missing credentials accepted", result, err)
		}
	}
	for _, invalid := range []*string{nil, stringPtr(""), stringPtr("owner/model:bad"), stringPtr(strings.Repeat("a", 63))} {
		if result, err := driver.Rerank(invalid, "q", []string{"d"}, &APIConfig{APIKey: stringPtr("key")}, nil); err == nil || result != nil {
			t.Fatal("invalid model accepted", result, err)
		}
	}
	missingEndpoint, err := factory.CreateModelDriver("Replicate", map[string]string{"default": server.URL}, URLSuffix{})
	if err != nil {
		t.Fatal(err)
	}
	if result, err := missingEndpoint.Rerank(&name, "q", []string{"d"}, &APIConfig{APIKey: stringPtr("key")}, nil); err == nil || result != nil {
		t.Fatal("missing prediction endpoint accepted", result, err)
	}
	if calls.Load() != 1 {
		t.Fatal("invalid input reached provider", calls.Load())
	}
}
