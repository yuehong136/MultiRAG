package cli

import (
	"encoding/json"
	"net/http"
	"path/filepath"
	"reflect"
	"testing"

	"multirag/internal/entity"
)

func TestReplicateCatalogCLIHTTP(t *testing.T) {
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	rows, err := manager.ListModels("Replicate")
	if err != nil {
		t.Fatal(err)
	}
	// Use the real catalog and CLI; the HTTP API boundary is controlled here.
	api := skillTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/api/v1/providers/Replicate/models" || r.Header.Get("Authorization") != "Bearer test-credential" {
			t.Error("catalog request", r.Method, r.URL.Path)
		}
		if err := json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": rows}); err != nil {
			t.Error(err)
		}
	})
	command, err := NewParser(`LIST MODELS FROM "Replicate";`).Parse(false)
	if err != nil {
		t.Fatal(err)
	}
	client := &MultiRAGClient{HTTPClient: api, ServerType: "user"}
	response, err := client.ExecuteUserCommand(command)
	if err != nil {
		t.Fatal(err)
	}
	result, ok := response.(*CommonResponse)
	if !ok {
		t.Fatalf("catalog response: %#v", response)
	}
	if len(result.Data) != 6 {
		t.Fatalf("catalog rows: %#v", result.Data)
	}
	want := map[string]string{
		"meta/llama-4-maverick-instruct": "chat",
		"meta/llama-4-scout-instruct":    "chat",
		"meta/meta-llama-3-70b-instruct": "chat",
		"meta/meta-llama-3-8b-instruct":  "chat",
		"replicate/all-mpnet-base-v2:b6b7585c9640cd7a9572c6e129c9549d79c9c31f0d3fdce7baac7c67ca38f305":                     "embedding",
		"ibm-granite/granite-embedding-278m-multilingual:1f76d42a05f120e12272746d5a2d86b525c13420773f795a4cbef9117d8685f1": "embedding",
	}
	for _, model := range result.Data {
		name, ok := model["name"].(string)
		typ, known := want[name]
		if !ok || !known || !reflect.DeepEqual(model["model_types"], []any{typ}) {
			t.Fatalf("catalog ID or capability changed: %#v", model)
		}
		delete(want, name)
	}
	if len(want) != 0 {
		t.Fatalf("catalog models missing: %#v", want)
	}
	cli, err := NewCLIWithArgs(&ConnectionArgs{Host: api.Host, Port: api.Port, APIToken: api.APIKey, OutputFormat: "json"})
	if err != nil {
		t.Fatal(err)
	}
	if err := cli.RunSingleCommand(`LIST MODELS FROM "Replicate";`); err != nil {
		t.Fatal(err)
	}
}
