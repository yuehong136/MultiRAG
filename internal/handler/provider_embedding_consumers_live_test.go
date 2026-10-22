package handler_test

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"gorm.io/gorm"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"

	"multirag/internal/cli"
	"multirag/internal/dao"
	"multirag/internal/entity"
)

// Uses the existing owned inference scratch fixture and registered HTTP handlers.
func checkEmbeddingProviderConsumers(t *testing.T, endpoint string, readback *sql.DB) {
	t.Helper()
	catalog := dao.GetModelProviderManager()
	gitee, err := catalog.GetModelByName("Gitee", "BAAI/bge-m3")
	if err != nil || !gitee.ModelTypeMap["embedding"] || gitee.MaxTokens != 8192 {
		t.Fatal("Gitee embedding catalog missing", err)
	}
	// Exercise an existing installation's obsolete globally unique credential index.
	if err := dao.DB.Exec("CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_model_instance_api_key ON tenant_model_instance(api_key)").Error; err != nil {
		t.Fatal(err)
	}
	if err := dao.DB.Migrator().DropIndex(&entity.TenantModelInstance{}, "idx_tenant_model_instance_api_key_nonempty"); err != nil {
		t.Fatal(err)
	}
	for range 2 {
		previous, err := dao.DB.DB()
		if err != nil {
			t.Fatal(err)
		}
		if err := dao.InitDB(); err != nil {
			t.Fatal("old-schema upgrade/repeated startup", err)
		}
		previous.Close()
	}
	current, err := dao.DB.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { current.Close() })
	var uniqueCredentials int
	if err := readback.QueryRow("SELECT count(*) FROM pg_indexes WHERE schemaname = current_schema() AND indexname = 'idx_tenant_model_instance_api_key'").Scan(&uniqueCredentials); err != nil || uniqueCredentials != 0 {
		t.Fatal("legacy credential uniqueness remains", uniqueCredentials, err)
	}
	var indexDefinition string
	if err := readback.QueryRow("SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND indexname = 'idx_tenant_model_instance_api_key_nonempty'").Scan(&indexDefinition); err != nil || !strings.Contains(indexDefinition, "WHERE") {
		t.Fatal("conditional unique index missing", indexDefinition, err)
	}
	for _, provider := range []string{"Gitee", "vllm", "ollama", "LM-Studio", "OpenRouter"} {
		t.Run(provider, func(t *testing.T) {
			providerID := "g-" + provider
			if err := dao.DB.Create(&entity.TenantModelProvider{ID: providerID, ProviderName: provider, TenantID: "owner-tenant"}).Error; err != nil {
				t.Fatal(err)
			}
			var calls atomic.Int32
			for i := range 2 {
				key := ""
				if (provider == "Gitee" || provider == "OpenRouter") || i == 1 {
					key = fmt.Sprintf("g-%s-%d", provider, i)
				}
				path := "/instance/v1/embeddings"
				basePath := "/instance/v1/"
				if provider == "ollama" {
					basePath = "/instance/"
				}
				listener := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					calls.Add(1)
					wantAuth := ""
					if key != "" {
						wantAuth = "Bearer " + key
					}
					if r.URL.Path != path || r.Header.Get("Authorization") != wantAuth || r.Method != "POST" {
						t.Error("provider instance endpoint/auth", r.URL.Path)
					}
					var body struct {
						Model      string
						Input      []string
						Dimensions int
						Query      *bool
					}
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
					}
					if body.Dimensions != 2 || body.Query != nil || body.Model == "" {
						t.Error(body)
					}
					if len(body.Input) == 1 && body.Input[0] == "bad" {
						fmt.Fprint(w, `{"data":[{"index":0,"embedding":[null,1]}]}`)
						return
					}
					data := make([]map[string]any, 0, len(body.Input))
					for j := len(body.Input) - 1; j >= 0; j-- {
						data = append(data, map[string]any{"index": j, "embedding": []float64{0, float64(i + 1)}})
					}
					json.NewEncoder(w).Encode(map[string]any{"data": data})
				}))
				t.Cleanup(listener.Close)
				extra, _ := json.Marshal(map[string]string{"region": "office", "base_url": listener.URL + basePath})
				id := fmt.Sprintf("g-%s-%d", provider, i)
				if err := dao.DB.Create(&entity.TenantModelInstance{ID: id, ProviderID: providerID, InstanceName: strconv.Itoa(i), APIKey: key, Status: "active", Extra: string(extra)}).Error; err != nil {
					t.Fatal(err)
				}
				if provider != "Gitee" {
					if err := dao.DB.Create(&entity.TenantModel{ID: id, ProviderID: providerID, InstanceID: id, ModelName: "local-embed", ModelType: "embedding", Status: "active", Extra: `{"max_tokens":8192,"model_types":["embedding"]}`}).Error; err != nil {
						t.Fatal(err)
					}
				}
			}
			snapshot := func() string {
				t.Helper()
				var value string
				if err := readback.QueryRow(`SELECT (SELECT md5(string_agg(row_to_json(t)::text,',' ORDER BY id)) FROM tenant_model_instance t)||':'||(SELECT count(*)::text FROM tenant_model)||':'||(SELECT count(*)::text FROM tenant_model_provider)`).Scan(&value); err != nil {
					t.Fatal(err)
				}
				return value
			}
			// Nonempty credentials retain their existing uniqueness contract.
			duplicate := &entity.TenantModelInstance{ID: "duplicate-" + provider, ProviderID: providerID, InstanceName: "duplicate", APIKey: fmt.Sprintf("g-%s-1", provider), Status: "active"}
			if err := dao.DB.Create(duplicate).Error; !errors.Is(err, gorm.ErrDuplicatedKey) {
				t.Fatal("nonempty duplicate key was not denied", err)
			}
			before := snapshot()
			parsed, _ := url.Parse(endpoint)
			port, _ := strconv.Atoi(parsed.Port())
			client := cli.NewMultiRAGClient("user")
			client.HTTPClient = cli.NewHTTPClient()
			client.HTTPClient.Host = parsed.Hostname()
			client.HTTPClient.Port = port
			client.HTTPClient.APIKey = "owner"
			model := "local-embed"
			if provider == "Gitee" {
				model = "BAAI/bge-m3"
			}
			for i := range 2 {
				command := "embed text " + strings.Repeat("'a' ", 33) + fmt.Sprintf("with '%s@%d@%s' dimension 2;", model, i, provider)
				cmd, err := cli.NewParser(command).Parse(false)
				if err != nil {
					t.Fatal(err)
				}
				result, err := client.ExecuteUserCommand(cmd)
				if err != nil {
					t.Fatal(err)
				}
				rows := result.(*cli.EmbeddingsResponse).Data
				if len(rows) != 33 || rows[32].Index != 32 || len(rows[32].Embedding) != 2 {
					t.Fatal("CLI embedding response", rows)
				}
			}
			if calls.Load() != 4 {
				t.Fatal("32-item batching missing", calls.Load())
			}
			bad, _ := cli.NewParser(fmt.Sprintf("embed text 'bad' with '%s@0@%s' dimension 2;", model, provider)).Parse(false)
			if _, err := client.ExecuteUserCommand(bad); err == nil {
				t.Fatal("malformed provider returned CLI success")
			}
			if calls.Load() != 5 {
				t.Fatal("bad response not exercised")
			}
			if err := dao.DB.Model(&entity.TenantModelInstance{}).Where("id = ?", "g-"+provider+"-0").Update("status", "inactive").Error; err != nil {
				t.Fatal(err)
			}
			deniedBefore := snapshot()
			if _, err := client.ExecuteUserCommand(bad); err == nil || calls.Load() != 5 {
				t.Fatal("disabled instance reached provider")
			}
			if snapshot() != deniedBefore {
				t.Fatal("disabled inference wrote SQL")
			}
			if err := dao.DB.Model(&entity.TenantModelInstance{}).Where("id = ?", "g-"+provider+"-0").Update("status", "active").Error; err != nil {
				t.Fatal(err)
			}
			client.HTTPClient.APIKey = "other"
			if _, err := client.ExecuteUserCommand(bad); err == nil || calls.Load() != 5 {
				t.Fatal("cross-tenant inference reached provider")
			}
			if after := snapshot(); after != before {
				t.Fatal("inference changed SQL", before, after)
			}
			t.Logf("CLI parser/client -> registered HTTP handler -> real scratch SQL binding -> two controlled provider listeners: %d calls; 33 texts split 32+1 per instance, keyless/keyed local, null rejected, disabled/cross-tenant zero calls; independent row hash unchanged", calls.Load())
		})
	}
}
