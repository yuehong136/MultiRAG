package service

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"sync/atomic"
	"testing"

	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/entity/models"
	"multirag/internal/server"
	"multirag/internal/testsupport"
	"multirag/internal/testsupport/scratch"
)

func canonicalModelDatabase(t *testing.T) *scratch.Services {
	t.Helper()
	s := scratch.New(t)
	_, source, _, _ := runtime.Caller(0)
	t.Chdir(filepath.Join(filepath.Dir(source), "..", ".."))
	configPath := filepath.Join(filepath.Dir(os.Getenv("MULTIRAG_GO_ALIGNMENT_CONFIG")), "service_conf.yaml")
	if err := server.Init(configPath); err != nil {
		t.Fatal("private canonical config initialization")
	}
	server.GetConfig().Database = s.Config.Database
	previous := dao.DB
	if err := dao.InitDB(); err != nil {
		t.Fatal(err)
	}
	pool, err := dao.DB.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close(); dao.DB = previous })
	active := "1"
	for _, id := range []string{"model-owner", "model-other"} {
		if err := dao.DB.Create(&entity.Tenant{ID: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := dao.DB.Create(&entity.UserTenant{ID: id, UserID: id, TenantID: id, Role: "owner", InvitedBy: id, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
	}
	return s
}

func TestCanonicalModelBindingsSQLHTTP(t *testing.T) {
	s := canonicalModelDatabase(t)
	svc := NewModelProviderService()
	var calls atomic.Int32
	var malformed atomic.Bool
	fixture := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if !strings.HasPrefix(r.URL.Path, "/one/") && !strings.HasPrefix(r.URL.Path, "/two/") {
			t.Error("instance endpoint lost")
			w.WriteHeader(400)
			return
		}
		instance := strings.Split(r.URL.Path, "/")[1]
		if r.Header.Get("Authorization") != "Bearer "+instance {
			t.Error("instance credentials lost")
			w.WriteHeader(401)
			return
		}
		if malformed.Load() {
			fmt.Fprint(w, `{"data":[{"id":17}]}`)
			return
		}
		switch {
		case strings.HasSuffix(r.URL.Path, "/models"):
			fmt.Fprint(w, `{"data":[{"id":"text-embedding-3-small"},{"id":"unknown-provider-model"}]}`)
		case strings.HasSuffix(r.URL.Path, "/embeddings"):
			var body struct {
				Model string   `json:"model"`
				Input []string `json:"input"`
			}
			if json.NewDecoder(r.Body).Decode(&body) != nil || body.Model != "text-embedding-3-small" {
				t.Error("bound model identity lost")
			}
			rows := make([]map[string]any, len(body.Input))
			for i := range rows {
				rows[i] = map[string]any{"index": i, "embedding": []float64{1, 2}}
			}
			json.NewEncoder(w).Encode(map[string]any{"data": rows})
		case strings.HasSuffix(r.URL.Path, "/chat/completions"):
			var body struct {
				Model    string           `json:"model"`
				Messages []models.Message `json:"messages"`
			}
			if json.NewDecoder(r.Body).Decode(&body) != nil || body.Model != "gpt-4o" || len(body.Messages) != 2 {
				t.Error("messages or model lost")
			}
			fmt.Fprintf(w, `{"choices":[{"message":{"content":%q}}]}`, instance)
		default:
			t.Error("unexpected provider path", r.URL.Path)
			w.WriteHeader(404)
		}
	}))
	providerName := "OpenAI"
	provider := dao.GetModelProviderManager().FindProvider(providerName)
	original, _ := json.Marshal(provider.URL)
	if code, err := svc.AddModelProvider(providerName, "model-owner"); err != nil || code != common.CodeSuccess {
		t.Fatal(code, err)
	}
	for _, instance := range []string{"one", "two"} {
		if code, err := svc.CreateProviderInstance(providerName, instance, instance, fixture.URL+"/"+instance, "default", "model-owner"); err != nil || code != common.CodeSuccess {
			t.Fatal(code, err)
		}
		if code, err := svc.AddModel(&AddModelRequest{ProviderName: providerName, InstanceName: instance, Models: []ModelRequest{{ModelName: "gpt-4o", ModelTypes: []string{"chat"}}, {ModelName: "text-embedding-3-small", ModelTypes: []string{"embedding"}}}}, "model-owner"); err != nil || code != common.CodeSuccess {
			t.Fatal(code, err)
		}
		rows, err := svc.ListSupportedModels(t.Context(), providerName, instance, "model-owner")
		if err != nil || len(rows) != 2 {
			t.Fatal("typed discovery duplicate/missing", len(rows), err)
		}
		if _, exists := rows[0]["max_dimension"]; !exists {
			t.Fatal("typed dimension metadata lost")
		}
		if _, exists := rows[0]["dimension"]; exists {
			t.Fatal("retired dimension alias returned")
		}
		name := "gpt-4o"
		response, code, err := svc.ChatToModelWithMessages(&providerName, &instance, &name, nil, "model-owner", []models.Message{{Role: "system", Content: "system"}, {Role: "user", Content: "q"}}, nil, nil)
		if err != nil || code != common.CodeSuccess || response == nil || response.Answer == nil || *response.Answer != instance {
			t.Fatal("canonical chat", code, err)
		}

		var modelID string
		if err := s.Read.QueryRow("SELECT m.id FROM tenant_model m JOIN tenant_model_instance i ON i.id=m.instance_id WHERE m.model_name=? AND i.instance_name=?", "gpt-4o", instance).Scan(&modelID); err != nil {
			t.Fatal(err)
		}
		byID, code, err := svc.ChatToModelWithMessages(nil, nil, nil, &modelID, "model-owner", []models.Message{{Role: "system", Content: "system"}, {Role: "user", Content: "q"}}, nil, nil)
		if err != nil || code != common.CodeSuccess || byID == nil || byID.Answer == nil || *byID.Answer != instance {
			t.Fatal("UUID modelID canonical route", code, err)
		}
		before := calls.Load()
		if result, _, err := svc.ChatToModelWithMessages(nil, nil, nil, &modelID, "model-other", []models.Message{{Role: "user", Content: "q"}}, nil, nil); err == nil || result != nil {
			t.Fatal("foreign UUID modelID accepted")
		}
		if calls.Load() != before {
			t.Fatal("foreign modelID reached provider")
		}
		emb, err := svc.GetEmbeddingModel("model-owner", "text-embedding-3-small@"+instance+"@OpenAI")
		if err != nil {
			t.Fatal(err)
		}
		vectors, err := embeddingVectorsForTest(emb.ModelDriver.Embed(emb.ModelName, []string{"query", "document"}, emb.APIConfig, nil))
		if err != nil || !reflect.DeepEqual(vectors, [][]float64{{1, 2}, {1, 2}}) {
			t.Fatal(vectors, err)
		}
	}
	current, _ := json.Marshal(provider.URL)
	if string(original) != string(current) {
		t.Fatal("instance mutated global catalog")
	}
	var count int
	if err := s.Read.QueryRow("SELECT COUNT(*) FROM tenant_model_instance").Scan(&count); err != nil || count != 2 {
		t.Fatal("independent SQL instance readback", count, err)
	}
	before := calls.Load()
	if _, err := svc.ListSupportedModels(t.Context(), providerName, "one", "model-other"); err == nil {
		t.Fatal("foreign tenant inventory exposed")
	}
	if _, err := svc.GetEmbeddingModel("model-other", "text-embedding-3-small@one@OpenAI"); err == nil {
		t.Fatal("foreign credentials resolved")
	}
	if _, err := svc.GetEmbeddingModel("model-owner", "gpt-4o@one@OpenAI"); err == nil {
		t.Fatal("wrong model type resolved")
	}
	if calls.Load() != before {
		t.Fatal("denied lookup reached provider")
	}
	var enrolled entity.TenantModel
	if err := dao.DB.Table("tenant_model").Select("tenant_model.*").Joins("JOIN tenant_model_instance i ON i.id = tenant_model.instance_id").Where("tenant_model.model_name = ? AND i.instance_name = ?", "text-embedding-3-small", "one").First(&enrolled).Error; err != nil {
		t.Fatal(err)
	}
	if err := dao.DB.Model(&enrolled).Update("status", "inactive").Error; err != nil {
		t.Fatal(err)
	}
	if _, err := svc.GetEmbeddingModel("model-owner", "text-embedding-3-small@one@OpenAI"); err == nil {
		t.Fatal("inactive model resolved")
	}
	if calls.Load() != before {
		t.Fatal("inactive model reached provider")
	}
	malformed.Store(true)
	if rows, err := svc.ListSupportedModels(t.Context(), providerName, "two", "model-owner"); err == nil || len(rows) != 0 {
		t.Fatal("malformed inventory returned partial success")
	}
}

func TestCanonicalModelIDOwnershipAndInputFailures(t *testing.T) {
	canonicalModelDatabase(t)
	svc := NewModelProviderService()
	for _, id := range []*string{nil, new(string)} {
		if result, err := svc.getModelInstanceAndProviderByID(id, "model-owner", nil); err == nil || result != nil {
			t.Fatal("invalid modelID accepted")
		}
	}
	for _, name := range []string{"", "model", "@provider", "model@", "model@@provider", "model@i@p@x"} {
		if _, _, _, err := parseModelName(name); err == nil {
			t.Fatalf("invalid identity %q accepted", name)
		}
	}
	if code, err := svc.AddModel(nil, "model-owner"); err == nil || code != common.CodeBadRequest {
		t.Fatal("nil enrollment accepted")
	}
	if code, err := svc.AddModel(&AddModelRequest{}, "model-owner"); err == nil || code != common.CodeBadRequest {
		t.Fatal("empty enrollment accepted")
	}
}
