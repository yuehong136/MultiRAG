//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//

package handler

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/testsupport/scratch"

	"multirag/internal/agent/runtime"
)

func init() {
	gin.SetMode(gin.TestMode)
}

// newAdminRuntimeTestRig uses owned real SQL/Redis. The direct handler rig
// supplies only the actor context; Router tests exercise the actual middleware.
func newAdminRuntimeTestRig(t *testing.T) (*gin.Engine, *runtime.Selector, *scratch.Services) {
	t.Helper()
	f := scratch.New(t)
	previous := dao.DB
	dao.DB = f.DB
	t.Cleanup(func() { dao.DB = previous })
	if err := f.DB.AutoMigrate(&entity.User{}, &entity.Tenant{}, &entity.UserTenant{}); err != nil {
		t.Fatal(err)
	}
	active := "1"
	actor := &entity.User{ID: "runtime-owner", Email: "runtime-owner@example.test", Nickname: "runtime owner", Status: &active, IsActive: "1"}
	if err := f.DB.Create(actor).Error; err != nil {
		t.Fatal(err)
	}
	for _, tenant := range []string{"tenant_123", "tenant_xyz", "tenant_1"} {
		if err := f.DB.Create(&entity.Tenant{ID: tenant, Status: &active}).Error; err != nil {
			t.Fatal(err)
		}
		if err := f.DB.Create(&entity.UserTenant{ID: tenant, TenantID: tenant, UserID: actor.ID, Role: "owner", Status: &active, InvitedBy: actor.ID}).Error; err != nil {
			t.Fatal(err)
		}
	}
	selector := runtime.NewSelector(f.Redis, nil)
	h := NewAdminRuntimeHandler(selector)
	f.Journal.Event("reserve-runtime-selector-keys", map[string]any{"redis_db": f.Config.Redis.DB, "keys": []string{"tenant_canvas_runtime:tenant_123", "tenant_canvas_runtime:tenant_xyz", "tenant_canvas_runtime:tenant_1"}})
	eng := gin.New()
	g := eng.Group("/api/v1/admin")
	g.Use(func(c *gin.Context) {
		if c.GetHeader("X-Test-Anonymous") == "" {
			c.Set("user", actor)
		}
		c.Next()
	})
	g.POST("/canvas-runtime/:tenant_id", h.SetTenantRuntime)
	return eng, selector, f
}

func TestAdminRuntime_SetGo(t *testing.T) {
	eng, selector, _ := newAdminRuntimeTestRig(t)

	body, _ := json.Marshal(map[string]string{"runtime": "go"})
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/canvas-runtime/tenant_123", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	eng.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
	}

	var resp setRuntimeResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if resp.Code != 0 || resp.TenantID != "tenant_123" || resp.Runtime != "go" {
		t.Errorf("unexpected response: %+v", resp)
	}

	// Round-trip: the selector should now report the override.
	mode, err := selector.Select(req.Context(), "tenant_123")
	if err != nil {
		t.Fatalf("Select(): %v", err)
	}
	if mode != runtime.RuntimeGo {
		t.Errorf("Select() after SetGo = %q, want %q", mode, runtime.RuntimeGo)
	}
}

func TestAdminRuntime_SetPython(t *testing.T) {
	eng, selector, _ := newAdminRuntimeTestRig(t)

	body, _ := json.Marshal(map[string]string{"runtime": "python"})
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/canvas-runtime/tenant_xyz", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	eng.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
	}

	mode, err := selector.Select(req.Context(), "tenant_xyz")
	if err != nil {
		t.Fatalf("Select(): %v", err)
	}
	if mode != runtime.RuntimePython {
		t.Errorf("Select() after SetPython = %q, want %q", mode, runtime.RuntimePython)
	}
}

func TestAdminRuntime_BadRequest(t *testing.T) {
	cases := []struct {
		name string
		body string
	}{
		{"unknown_mode", `{"runtime":"rust"}`},
		{"empty_mode", `{"runtime":""}`},
		{"malformed_json", `{"runtime":`},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			eng, _, _ := newAdminRuntimeTestRig(t)
			req := httptest.NewRequest(http.MethodPost,
				"/api/v1/admin/canvas-runtime/tenant_1",
				bytes.NewReader([]byte(tc.body)))
			req.Header.Set("Content-Type", "application/json")
			w := httptest.NewRecorder()
			eng.ServeHTTP(w, req)

			if w.Code != http.StatusOK {
				t.Fatalf("status = %d, want 200 envelope", w.Code)
			}
			var env map[string]any
			if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
				t.Fatalf("decode: %v", err)
			}
			// 101 == CodeArgumentError, the only acceptable error for bad input.
			if code, _ := env["code"].(float64); code != 101 {
				t.Errorf("code = %v, want 101 (CodeArgumentError); body=%s", env["code"], w.Body.String())
			}
		})
	}
}

func TestAdminRuntime_RejectsUnauthorizedWithoutRedisWrites(t *testing.T) {
	for _, mode := range []string{"anonymous", "foreign", "normal", "invite", "revoked", "disabled-user", "deleted-user", "disabled-tenant"} {
		t.Run(mode, func(t *testing.T) {
			eng, _, f := newAdminRuntimeTestRig(t)
			switch mode {
			case "foreign":
				if err := f.DB.Where("tenant_id=?", "tenant_123").Delete(&entity.UserTenant{}).Error; err != nil {
					t.Fatal(err)
				}
			case "normal", "invite":
				if err := f.DB.Model(&entity.UserTenant{}).Where("tenant_id=?", "tenant_123").Update("role", mode).Error; err != nil {
					t.Fatal(err)
				}
			case "revoked":
				if err := f.DB.Model(&entity.UserTenant{}).Where("tenant_id=?", "tenant_123").Update("status", "0").Error; err != nil {
					t.Fatal(err)
				}
			case "disabled-user":
				if err := f.DB.Model(&entity.User{}).Where("id=?", "runtime-owner").Update("is_active", "0").Error; err != nil {
					t.Fatal(err)
				}
			case "deleted-user":
				if err := f.DB.Model(&entity.User{}).Where("id=?", "runtime-owner").Update("status", "0").Error; err != nil {
					t.Fatal(err)
				}
			case "disabled-tenant":
				if err := f.DB.Model(&entity.Tenant{}).Where("id=?", "tenant_123").Update("status", "0").Error; err != nil {
					t.Fatal(err)
				}
			}
			req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/canvas-runtime/tenant_123", bytes.NewBufferString(`{"runtime":"go"}`))
			req.Header.Set("Content-Type", "application/json")
			if mode == "anonymous" {
				req.Header.Set("X-Test-Anonymous", "1")
			}
			response := httptest.NewRecorder()
			eng.ServeHTTP(response, req)
			want := http.StatusForbidden
			if mode == "anonymous" {
				want = http.StatusUnauthorized
			}
			if response.Code != want {
				t.Fatal(mode, response.Code, response.Body.String())
			}
			reader := redis.NewClient(f.Redis.Options())
			defer reader.Close()
			if count, err := reader.DBSize(t.Context()).Result(); err != nil || count != 0 {
				t.Fatal("denied runtime write reached Redis", count, err)
			}
		})
	}
}
