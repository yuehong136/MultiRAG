package cli

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"sync/atomic"
	"testing"

	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/testsupport"
)

func TestVolcEngineAliyunSupportedModelsCLIHTTP(t *testing.T) {
	for _, provider := range []string{"VolcEngine", "Aliyun"} {
		t.Run(provider, func(t *testing.T) {
			var fail atomic.Bool
			upstream := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodGet || r.URL.Path != "/models" || r.Header.Get("Authorization") != "Bearer provider-key" {
					t.Errorf("unexpected provider request: %s %s", r.Method, r.URL.Path)
				}
				if fail.Load() {
					fmt.Fprint(w, `{"error":{"message":"provider failure"}}`)
					return
				}
				fmt.Fprint(w, `{"data":[{"id":"model-b","owned_by":"vendor"},{"id":"model-a"}]}`)
			}))
			defer upstream.Close()
			driver, err := models.NewModelFactory().CreateModelDriver(provider, map[string]string{"default": upstream.URL}, models.URLSuffix{Models: "models"})
			if err != nil {
				t.Fatal(err)
			}
			key := "provider-key"
			journal := testsupport.NewCase(t)
			catalogPath := filepath.Join(t.TempDir(), "catalog.json")
			journal.Event("reserve-catalog", map[string]any{"path": catalogPath})
			if err := os.WriteFile(catalogPath, []byte(`[{"name":"catalog-a","alias":["model-a"],"max_tokens":0,"dimension":0,"model_types":[],"thinking":{"default_value":false,"clear_thinking":false}}]`), 0600); err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() {
				if err := os.Remove(catalogPath); err != nil {
					t.Error(err)
				}
				_, err := os.Stat(catalogPath)
				journal.Event("catalog-cleanup-readback", map[string]any{"path": catalogPath, "absent": os.IsNotExist(err)})
			})
			catalog := &entity.ProviderManager{}
			if err := catalog.LoadAllModels(catalogPath); err != nil {
				t.Fatal(err)
			}
			// The API boundary is controlled here; SQL-backed handler acceptance is separate.
			api := skillTestClient(t, func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodGet || r.URL.Path != "/api/v1/providers/"+provider+"/instances/primary/models" || r.URL.Query().Get("supported") != "true" {
					t.Errorf("unexpected API request: %s %s", r.Method, r.URL)
				}
				names, err := driver.ListModels(&models.APIConfig{APIKey: &key})
				if err != nil {
					fmt.Fprint(w, `{"code":100,"message":"Provider model listing failed"}`)
					return
				}
				rows := catalog.DescribeSupportedModels(provider, names)
				if err := json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": rows}); err != nil {
					t.Error(err)
				}
			})
			client := &MultiRAGClient{HTTPClient: api, ServerType: "user"}
			command, err := NewParser(fmt.Sprintf(`LIST SUPPORTED MODELS FROM %q "primary";`, provider)).Parse(false)
			if err != nil {
				t.Fatal(err)
			}
			response, err := client.ExecuteUserCommand(command)
			if err != nil {
				t.Fatal(err)
			}
			result, ok := response.(*CommonResponse)
			want := []map[string]any{{"name": "model-b"}, {"name": "model-a", "max_tokens": float64(0), "dimension": float64(0), "model_types": []any{}, "thinking": map[string]any{"default_value": false, "clear_thinking": false}}}
			if !ok || !reflect.DeepEqual(result.Data, want) {
				t.Fatalf("response = %#v", response)
			}
			// The real public entry consumes the typed catalog output through HTTP.
			cli, err := NewCLIWithArgs(&ConnectionArgs{Host: api.Host, Port: api.Port, APIToken: api.APIKey, OutputFormat: "json"})
			if err != nil {
				t.Fatal(err)
			}
			if err := cli.RunSingleCommand(fmt.Sprintf(`LIST SUPPORTED MODELS FROM %q "primary";`, provider)); err != nil {
				t.Fatal(err)
			}
			fail.Store(true)
			if response, err := client.ExecuteUserCommand(command); err == nil || response != nil {
				t.Fatalf("provider failure reported success: %#v, %v", response, err)
			}
		})
	}
}
