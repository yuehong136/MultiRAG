package handler_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"multirag/internal/dao"
	"multirag/internal/handler"
	"multirag/internal/server"
	"multirag/internal/service"
	"multirag/internal/testsupport"
	"multirag/internal/testsupport/scratch"
)

func TestCanonicalProviderCheckCancellationSQLHTTP(t *testing.T) {
	s := scratch.New(t)
	_, source, _, _ := runtime.Caller(0)
	t.Chdir(filepath.Join(filepath.Dir(source), "..", ".."))
	configPath := filepath.Join(filepath.Dir(os.Getenv("MULTIRAG_GO_ALIGNMENT_CONFIG")), "service_conf.yaml")
	if err := server.Init(configPath); err != nil {
		t.Fatal("initialize private canonical configuration")
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
	counts := func() [3]int {
		t.Helper()
		var result [3]int
		for i, table := range []string{"tenant_model_provider", "tenant_model_instance", "tenant_model"} {
			if err := s.Read.QueryRowContext(t.Context(), "SELECT count(*) FROM "+table).Scan(&result[i]); err != nil {
				t.Fatal(err)
			}
		}
		return result
	}
	before := counts()
	started, stopped, handled := make(chan struct{}), make(chan struct{}), make(chan struct{})
	release := make(chan struct{})
	defer close(release)
	upstream := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/models" || r.Header.Get("Authorization") != "Bearer controlled-probe-key" {
			t.Error("canonical provider probe route or credentials changed")
		}
		w.WriteHeader(http.StatusOK)
		w.(http.Flusher).Flush()
		close(started)
		select {
		case <-r.Context().Done():
			close(stopped)
		case <-release:
		}
	}))
	gin.SetMode(gin.TestMode)
	routes := gin.New()
	// Authentication is a controlled boundary here; the actual HTTP request,
	// canonical handler, service, driver and independently observed SQL are real.
	routes.Use(func(c *gin.Context) { c.Set("user_id", "controlled-owner") })
	providers := handler.NewProviderHandler(nil, service.NewModelProviderService())
	routes.POST("/api/v1/providers/:provider_name/connection", providers.CheckConnection)
	api := testsupport.NewHTTP(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer close(handled)
		routes.ServeHTTP(w, r)
	}))
	body, err := json.Marshal(service.CheckConnectionRequest{APIKey: "controlled-probe-key", Region: "default", BaseURL: upstream.URL})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, api.URL+"/api/v1/providers/lmstudio/connection", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Content-Type", "application/json")
	returned := make(chan error, 1)
	go func() {
		response, err := api.Client().Do(request)
		if response != nil {
			response.Body.Close()
		}
		returned <- err
	}()
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("handler did not reach the controlled provider")
	}
	cancel()
	for name, completed := range map[string]<-chan struct{}{"provider request": stopped, "handler": handled} {
		select {
		case <-completed:
		case <-time.After(5 * time.Second):
			t.Fatal(name, "did not stop after request cancellation")
		}
	}
	if err := <-returned; !errors.Is(err, context.Canceled) {
		t.Fatal("request cancellation was replaced with success", err)
	}
	if after := counts(); after != before {
		t.Fatal("cancelled probe changed model state in independent SQL", before, after)
	}
}
