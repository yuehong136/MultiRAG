package skills

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"

	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
	"multirag/internal/entity"
)

func TestLegacyEmbeddingScratchPostgres(t *testing.T) {
	dsn := os.Getenv("MULTIRAG_SKILL_EMBEDDING_DSN")
	if dsn == "" {
		if os.Getenv("REQUIRE_SERVICES") == "1" {
			t.Fatal("owned skill embedding scratch database required")
		}
		t.Skip("requires owned skill embedding scratch database")
	}
	u, err := url.Parse(dsn)
	if err != nil || !strings.HasPrefix(strings.TrimPrefix(u.Path, "/"), "multirag_go_models_a_skills_") {
		t.Fatal("scratch database is not owned")
	}
	db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		t.Fatal("connect scratch failed")
	}
	pool, _ := db.DB()
	defer pool.Close()
	if err := db.AutoMigrate(&entity.TenantLLM{}); err != nil {
		t.Fatal(err)
	}
	readback, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal("independent connection failed")
	}
	defer readback.Close()
	var calls atomic.Int32
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var body struct{ Input []string }
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if r.URL.Path != "/embeddings" || r.Header.Get("Authorization") != "Bearer fixture" {
			t.Error("binding lost endpoint or credential")
		}
		if len(body.Input) == 1 && body.Input[0] == "bad" {
			fmt.Fprint(w, `{"data":[{"index":0,"embedding":[null,1]}]}`)
			return
		}
		fmt.Fprint(w, `{"data":[{"index":1,"embedding":[2,0]},{"index":0,"embedding":[0,1]}]}`)
	}))
	defer provider.Close()
	name, key, base, typ := "model", "fixture", provider.URL, "embedding"
	row := entity.TenantLLM{TenantID: "owner", LLMFactory: "OpenAI-API-Compatible", LLMName: &name, APIKey: &key, APIBase: &base, ModelType: &typ, Status: "1"}
	if err := db.Create(&row).Error; err != nil {
		t.Fatal(err)
	}
	manager, err := entity.NewProviderManager(filepath.Join("..", "..", "configs", "models"))
	if err != nil {
		t.Fatal(err)
	}
	models := LegacyModels{DB: db, Providers: manager}
	snapshot := func() string {
		t.Helper()
		var hash string
		if err := readback.QueryRow("SELECT md5(row_to_json(t)::text) FROM t_ai_tenant_llms t WHERE id=$1", row.ID).Scan(&hash); err != nil {
			t.Fatal(err)
		}
		return hash
	}
	before := snapshot()
	for _, query := range []bool{false, true} {
		vectors, err := models.Embed(context.Background(), "owner", row.ID, []string{"first", "second"}, query)
		if err != nil || !reflect.DeepEqual(vectors, [][]float64{{0, 1}, {2, 0}}) {
			t.Fatal(vectors, err)
		}
	}
	if _, err := models.Embed(context.Background(), "owner", row.ID, []string{"bad"}, false); err == nil {
		t.Fatal("null vector accepted")
	}
	if _, err := models.Embed(context.Background(), "other", row.ID, []string{"first"}, true); err == nil || calls.Load() != 3 {
		t.Fatal("cross-tenant request reached provider")
	}
	if snapshot() != before {
		t.Fatal("inference mutated SQL")
	}
	if err := db.Model(&row).Update("status", "0").Error; err != nil {
		t.Fatal(err)
	}
	disabled := snapshot()
	if _, err := models.Embed(context.Background(), "owner", row.ID, []string{"first"}, false); err == nil || calls.Load() != 3 || snapshot() != disabled {
		t.Fatal("disabled inference had forbidden side effects")
	}
	t.Log("legacy skill adapter: ordered document/query vectors, null rejection, cross-tenant and disabled zero calls; independent SQL row hashes unchanged")
}
