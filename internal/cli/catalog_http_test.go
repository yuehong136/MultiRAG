package cli

import (
	"encoding/json"
	"math"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/service"
	"multirag/internal/testsupport"
)

// TestCatalogCLIHTTP runs the real parser, dispatch, HTTP transport, handler,
// service and catalog. Production user/admin middleware is verified by router tests.
func TestCatalogCLIHTTP(t *testing.T) {
	j := testsupport.NewCase(t)
	path := filepath.Join(os.TempDir(), "catalog-cli-"+strconv.Itoa(os.Getpid())+".json")
	j.Event("reserve-catalog-file", map[string]any{"path": path})
	raw := []byte(`[{"name":"canonical","alias":["组织/a+b?"],"model_types":["embedding"],"max_tokens":0},{"name":"second","model_types":["tts"]},{"name":"last","model_types":["chat"]}]`)
	f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := os.Remove(path); err != nil {
			t.Error(err)
		}
		_, err := os.Stat(path)
		j.Event("catalog-file-cleanup", map[string]any{"path": path, "absent": os.IsNotExist(err)})
	})
	if _, err = f.Write(raw); err != nil {
		t.Fatal(err)
	}
	if err = f.Sync(); err != nil {
		t.Fatal(err)
	}
	if err = f.Close(); err != nil {
		t.Fatal(err)
	}
	if stored, err := os.ReadFile(path); err != nil || string(stored) != string(raw) {
		t.Fatal("fixture readback failed")
	}
	manager := &entity.ProviderManager{}
	if err := manager.LoadAllModels(path); err != nil {
		t.Fatal(err)
	}
	models := service.NewModelProviderServiceWithCatalog(manager, nil)
	h := handler.NewModelHandler(models)
	engine := gin.New()
	for _, prefix := range []string{"/api/v1", "/api/v1/admin"} {
		group := engine.Group(prefix)
		group.Use(func(c *gin.Context) {
			if c.GetHeader("Authorization") != "fixture" {
				c.AbortWithStatus(http.StatusUnauthorized)
				return
			}
			c.Set("user", &entity.User{ID: "fixture"})
		})
		group.GET("/all-models", h.ListAllModels)
		group.GET("/all-models/detail", h.ShowModel)
	}
	server := providerKeyServer(t, engine)
	u, _ := url.Parse(server.URL)
	port, _ := strconv.Atoi(u.Port())
	for _, mode := range []string{"user", "admin"} {
		client := NewMultiRAGClient(mode)
		client.HTTPClient.Host = u.Hostname()
		client.HTTPClient.Port = port
		client.HTTPClient.LoginToken = "fixture"
		cmd, err := NewParser(`LIST MODELS;`).Parse(mode == "admin")
		if err != nil {
			t.Fatal(err)
		}
		result, err := client.ExecuteCommand(cmd)
		if err != nil {
			t.Fatal(err)
		}
		list := result.(*CommonResponse)
		if len(list.Data) != 3 || list.Data[0]["max_tokens"] != float64(0) {
			t.Fatalf("list lost metadata: %+v", list)
		}
		for _, name := range []string{"canonical", "组织/a+b?"} {
			cmd, err := NewParser("SHOW MODEL '" + name + "';").Parse(mode == "admin")
			if err != nil {
				t.Fatal(err)
			}
			response, err := client.ExecuteCommand(cmd)
			if err != nil {
				t.Fatal(err)
			}
			if response.(*CommonDataResponse).Data["name"] != "canonical" {
				t.Fatal("alias/canonical mismatch")
			}
		}
		cmd, _ = NewParser(`SHOW MODEL 'unknown';`).Parse(mode == "admin")
		if _, err := client.ExecuteCommand(cmd); err == nil {
			t.Fatal("unknown resolved to first model")
		}
	}
	for _, tc := range []struct {
		query string
		count int
		ok    bool
	}{
		{"", 3, true}, {"?page=1&page_size=2", 1, true}, {"?page=2&page_size=2", 0, true},
		{"?page=-1", 0, false}, {"?page_size=-1", 0, false}, {"?page=1", 0, false},
		{"?page_size=", 0, false}, {"?page=0&page=1", 0, false}, {"?page=%zz", 0, false},
		{"?page=9999999999999999999999999", 0, false}, {"?page=" + strconv.Itoa(math.MaxInt) + "&page_size=2", 0, false},
	} {
		req := httptest.NewRequest("GET", "/api/v1/all-models"+tc.query, nil)
		req.Header.Set("Authorization", "fixture")
		rec := httptest.NewRecorder()
		engine.ServeHTTP(rec, req)
		var body struct {
			Code  int               `json:"code"`
			Data  []json.RawMessage `json:"data"`
			Total int               `json:"total"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
			t.Fatal(err)
		}
		if tc.ok {
			if body.Code != 0 || len(body.Data) != tc.count || body.Total != 3 {
				t.Fatalf("%s: %s", tc.query, rec.Body)
			}
		} else if body.Code == 0 {
			t.Fatalf("accepted %s", tc.query)
		}
	}
	if _, _, err := models.ListAllModels(math.MaxInt, 2); err == nil {
		t.Fatal("service accepted overflow")
	}
	empty := service.NewModelProviderServiceWithCatalog(&entity.ProviderManager{}, nil)
	list, total, err := empty.ListAllModels(0, 0)
	if err != nil || total != 0 || list == nil || len(list) != 0 {
		t.Fatal("empty catalog must return []/0")
	}
	for _, target := range []string{"/api/v1/all-models", "/api/v1/admin/all-models"} {
		rec := httptest.NewRecorder()
		engine.ServeHTTP(rec, httptest.NewRequest("GET", target, nil))
		if rec.Code != 401 {
			t.Fatal("unauthenticated catalog accepted")
		}
	}
}
