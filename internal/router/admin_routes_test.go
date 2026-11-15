package router

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/agent/runtime"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/handler"
	"multirag/internal/testsupport/scratch"
)

func adminRouteActor(t *testing.T) (*scratch.Services, *entity.User) {
	t.Helper()
	f := scratch.New(t)
	previous := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = previous })
	if err := f.DB.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}); err != nil {
		t.Fatal(err)
	}
	active := "1"
	id := f.Prefix + "owner"
	user := &entity.User{ID: id, Email: id + "@example.test", Nickname: "owner", Status: &active, IsActive: "1"}
	f.Journal.Event("reserve-admin-route-membership", map[string]interface{}{"user": id, "tenant": id, "role": "owner"})
	for _, row := range []interface{}{user, &entity.Tenant{ID: id, Status: &active}, &entity.UserTenant{ID: f.Prefix + "relation", TenantID: id, UserID: id, InvitedBy: id, Role: "owner", Status: &active}} {
		if err := f.DB.Create(row).Error; err != nil {
			t.Fatal(err)
		}
	}
	return f, user
}
func TestAdminRuntimeRoutes_Registered(t *testing.T) {
	f, user := adminRouteActor(t)
	selector := runtime.NewSelector(f.Redis, nil)
	h := handler.NewAdminRuntimeHandler(selector)
	eng := gin.New()
	eng.Use(func(c *gin.Context) { c.Set("user", user); c.Next() })
	RegisterAdminRuntimeRoutes(eng.Group("/api/v1/admin"), h)
	f.Journal.Event("reserve-runtime-override", map[string]interface{}{"key": "tenant_canvas_runtime:" + user.ID})
	body, _ := json.Marshal(map[string]string{"runtime": "go"})
	rec := httptest.NewRecorder()
	eng.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/api/v1/admin/canvas-runtime/"+user.ID, bytes.NewReader(body)))
	if rec.Code != 200 || !bytes.Contains(rec.Body.Bytes(), []byte(`"runtime":"go"`)) {
		t.Fatal(rec.Code, rec.Body.String())
	}
	if value, err := f.Redis.Get(t.Context(), "tenant_canvas_runtime:"+user.ID).Result(); err != nil || value != "go" {
		t.Fatal("independent Redis readback", value, err)
	}
}
func TestAdminRuntimeRoutes_NilSafety(t *testing.T) { RegisterAdminRuntimeRoutes(nil, nil) }
func TestAdminRuntimeRoutes_StaysRegisteredWithNilSelector(t *testing.T) {
	_, user := adminRouteActor(t)
	eng := gin.New()
	eng.Use(func(c *gin.Context) { c.Set("user", user); c.Next() })
	RegisterAdminRuntimeRoutes(eng.Group("/api/v1/admin"), handler.NewAdminRuntimeHandler(nil))
	rec := httptest.NewRecorder()
	eng.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/api/v1/admin/canvas-runtime/"+user.ID, bytes.NewBufferString(`{"runtime":"go"}`)))
	var result struct {
		Code int `json:"code"`
	}
	if json.Unmarshal(rec.Body.Bytes(), &result) != nil || result.Code == 0 || rec.Code != 200 || !bytes.Contains(rec.Body.Bytes(), []byte("selector not configured")) {
		t.Fatal("configured route must explicitly refuse missing selector", rec.Code, rec.Body.String())
	}
}
