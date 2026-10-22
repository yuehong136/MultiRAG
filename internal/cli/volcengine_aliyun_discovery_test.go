package cli

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sync/atomic"
	"testing"

	"multirag/internal/entity/models"
)

func TestVolcEngineAliyunSupportedModelsCLIHTTP(t *testing.T) {
	for _, provider := range []string{"VolcEngine", "Aliyun"} {
		t.Run(provider, func(t *testing.T) {
			var fail atomic.Bool
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
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
				rows := make([]map[string]string, 0, len(names))
				for _, name := range names {
					rows = append(rows, map[string]string{"model_name": name})
				}
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
			want := []map[string]any{{"model_name": "model-b"}, {"model_name": "model-a"}}
			if !ok || !reflect.DeepEqual(result.Data, want) {
				t.Fatalf("response = %#v", response)
			}
			fail.Store(true)
			if response, err := client.ExecuteUserCommand(command); err == nil || response != nil {
				t.Fatalf("provider failure reported success: %#v, %v", response, err)
			}
		})
	}
}
