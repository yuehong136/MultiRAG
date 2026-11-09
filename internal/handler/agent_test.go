//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

package handler

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/glebarez/sqlite"
	"gorm.io/gorm"

	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"multirag/internal/service"
)

// setupHandlerAgentsTestDB sets up SQLite in-memory DB with tables needed for agent handler tests.
func setupHandlerAgentsTestDB(t *testing.T) *gorm.DB {
	t.Helper()

	db, err := gorm.Open(sqlite.Open(":memory:"), &gorm.Config{
		TranslateError: true,
	})
	if err != nil {
		t.Fatalf("failed to open sqlite: %v", err)
	}

	if err := db.AutoMigrate(
		&entity.User{},
		&entity.UserCanvas{},
		&entity.UserCanvasVersion{},
		&entity.UserTenant{},
		&entity.API4Conversation{},
	); err != nil {
		t.Fatalf("failed to migrate: %v", err)
	}

	return db
}

// setupGinContextWithUserAndDB creates a gin context with pre-authenticated user
// and swaps dao.DB to the test database. Returns cleanup function.
func setupGinContextWithUserAndDB(t *testing.T, method, path string) (*gin.Context, *httptest.ResponseRecorder, *gorm.DB) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(method, path, nil)
	c.Set("user", &entity.User{ID: "user-1"})
	c.Set("user_id", "user-1")

	db := setupHandlerAgentsTestDB(t)
	orig := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = orig })

	return c, w, db
}

// TestListAgentVersionsHandler_Success verifies the happy path with real DB.
func TestListAgentVersionsHandler_Success(t *testing.T) {
	c, w, db := setupGinContextWithUserAndDB(t, "GET", "/api/v1/agents/canvas-1/versions")
	c.Params = gin.Params{{Key: "canvas_id", Value: "canvas-1"}}

	// Insert canvas owned by user-1
	db.Create(&entity.UserCanvas{
		ID:     "canvas-1",
		UserID: "user-1",
		Title:  agentString("Test Agent"),
	})

	// Insert 2 versions with staggered timestamps
	now := time.Now()
	db.Create(&entity.UserCanvasVersion{
		ID:           "v2",
		UserCanvasID: "canvas-1",
		Title:        agentString("v2"),
		BaseModel: entity.BaseModel{
			CreateTime: ptr(now.UnixMilli()), UpdateTime: ptr(now.UnixMilli()),
		},
	})
	db.Create(&entity.UserCanvasVersion{
		ID:           "v1",
		UserCanvasID: "canvas-1",
		Title:        agentString("v1"),
		BaseModel: entity.BaseModel{
			CreateTime: ptr(now.Add(-time.Hour).UnixMilli()), UpdateTime: ptr(now.Add(-time.Hour).UnixMilli()),
		},
	})

	h := NewAgentHandler(service.NewAgentService(), nil)
	h.ListVersions(c)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", w.Code, w.Body.String())
	}

	var resp map[string]interface{}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("failed to parse response: %v", err)
	}

	code, _ := resp["code"].(float64)
	if code != float64(common.CodeSuccess) {
		t.Fatalf("expected code 0, got %v: %v", code, resp["message"])
	}

	data, ok := resp["data"].([]interface{})
	if !ok {
		t.Fatalf("expected data array, got %T", resp["data"])
	}
	if len(data) != 2 {
		t.Fatalf("expected 2 versions, got %d", len(data))
	}

	v2 := data[0].(map[string]interface{})
	if v2["title"] != "v2" {
		t.Errorf("expected v2 first, got %s", v2["title"])
	}
}

// TestListAgentVersionsHandler_NoPermission verifies cross-user access is denied.
func TestListAgentVersionsHandler_NoPermission(t *testing.T) {
	gin.SetMode(gin.TestMode)

	db := setupHandlerAgentsTestDB(t)
	orig := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = orig })

	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest("GET", "/api/v1/agents/canvas-b/versions", nil)
	c.Set("user", &entity.User{ID: "user-a"})
	c.Set("user_id", "user-a")
	c.Params = gin.Params{{Key: "canvas_id", Value: "canvas-b"}}

	// Canvas owned by user-b
	db.Create(&entity.UserCanvas{ID: "canvas-b", UserID: "user-b", Title: agentString("Not Yours")})

	h := NewAgentHandler(service.NewAgentService(), nil)
	h.ListVersions(c)

	var resp map[string]interface{}
	json.Unmarshal(w.Body.Bytes(), &resp)
	code, _ := resp["code"].(float64)
	// mapAgentError now maps ErrUserCanvasNotFound -> 103 "Make sure you
	// have permission to access the agent." to mirror the Python agent
	// API's _require_canvas_access_sync decorator.
	if code != float64(common.CodeOperatingError) {
		t.Errorf("expected access-denied code %d, got %v", common.CodeOperatingError, code)
	}
}

// TestListAgentVersionsHandler_CanvasNotFound verifies behavior for non-existent canvas.
func TestListAgentVersionsHandler_CanvasNotFound(t *testing.T) {
	gin.SetMode(gin.TestMode)

	db := setupHandlerAgentsTestDB(t)
	orig := dao.DB
	dao.DB = db
	t.Cleanup(func() { dao.DB = orig })

	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest("GET", "/api/v1/agents/non-existent/versions", nil)
	c.Set("user", &entity.User{ID: "user-1"})
	c.Set("user_id", "user-1")
	c.Params = gin.Params{{Key: "canvas_id", Value: "non-existent"}}

	h := NewAgentHandler(service.NewAgentService(), nil)
	h.ListVersions(c)

	var resp map[string]interface{}
	json.Unmarshal(w.Body.Bytes(), &resp)
	code, _ := resp["code"].(float64)
	// mapAgentError now maps ErrUserCanvasNotFound -> 103 "Make sure you
	// have permission to access the agent." to mirror the Python agent
	// API's _require_canvas_access_sync decorator.
	if code != float64(common.CodeOperatingError) {
		t.Errorf("expected access-denied code %d, got %v", common.CodeOperatingError, code)
	}
}

// TestGetAgentVersionHandler_Success verifies getting a specific version.
func TestGetAgentVersionHandler_Success(t *testing.T) {
	c, w, db := setupGinContextWithUserAndDB(t, "GET", "/api/v1/agents/canvas-1/versions/v1")
	c.Params = gin.Params{{Key: "canvas_id", Value: "canvas-1"}, {Key: "version_id", Value: "v1"}}

	db.Create(&entity.UserCanvas{
		ID:     "canvas-1",
		UserID: "user-1",
		Title:  agentString("Test Agent"),
	})
	db.Create(&entity.UserCanvasVersion{
		ID:           "v1",
		UserCanvasID: "canvas-1",
		Title:        agentString("version-1"),
		DSL:          entity.JSONMap{"key": "value"},
	})

	h := NewAgentHandler(service.NewAgentService(), nil)
	h.GetVersion(c)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", w.Code, w.Body.String())
	}

	var resp map[string]interface{}
	json.Unmarshal(w.Body.Bytes(), &resp)
	code, _ := resp["code"].(float64)
	if code != float64(common.CodeSuccess) {
		t.Fatalf("expected code 0, got %v: %v", code, resp["message"])
	}

	data, ok := resp["data"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected data object, got %T", resp["data"])
	}
	if data["title"] != "version-1" {
		t.Errorf("expected title 'version-1', got %v", data["title"])
	}
	if _, ok := data["dsl"]; !ok {
		t.Errorf("expected dsl field in version detail response")
	}
}

// TestGetAgentVersionHandler_VersionNotFound verifies 404 for missing version.
func TestGetAgentVersionHandler_VersionNotFound(t *testing.T) {
	c, w, db := setupGinContextWithUserAndDB(t, "GET", "/api/v1/agents/canvas-1/versions/non-existent")
	c.Params = gin.Params{{Key: "canvas_id", Value: "canvas-1"}, {Key: "version_id", Value: "non-existent"}}

	db.Create(&entity.UserCanvas{
		ID:     "canvas-1",
		UserID: "user-1",
		Title:  agentString("Test Agent"),
	})

	h := NewAgentHandler(service.NewAgentService(), nil)
	h.GetVersion(c)

	var resp map[string]interface{}
	json.Unmarshal(w.Body.Bytes(), &resp)
	code, _ := resp["code"].(float64)
	// mapAgentError now maps ErrUserCanvasVersionNotFound -> 103 to match
	// the user_canvas_not_found behaviour (same 403 envelope).
	if code != float64(common.CodeOperatingError) {
		t.Errorf("expected access-denied code %d, got %v", common.CodeOperatingError, code)
	}
}

// sptr returns a pointer to the given string.
// ptr returns a pointer to the given int64.
func ptr(v int64) *int64 { return &v }

// fakeAgentService satisfies the subset of AgentService used by the handler.
// It is injected via a wrapper to avoid importing the real DAO (which requires a DB).
func TestAgentHandler_NotFoundOnUnknownCanvas(t *testing.T) {
	c, w, _ := setupGinContextWithUserAndDB(t, "GET", "/api/v1/agents/unknown")
	c.Params = gin.Params{{Key: "canvas_id", Value: "unknown"}}
	NewAgentHandler(service.NewAgentService(), nil).GetAgent(c)
	var body map[string]interface{}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body["code"] != float64(common.CodeOperatingError) {
		t.Fatalf("unknown canvas leaked existence: %s", w.Body)
	}
}

// TestMapAgentError covers every error path mapAgentError handles.
//
// IDOR mitigation: ErrUserCanvasNotFound and ErrUserCanvasVersionNotFound
// both surface as CodeOperatingError(103) with the same "Make sure you
// have permission to access the agent." message, so the front-end
// cannot use the response code to distinguish a missing canvas from a
// foreign one. This matches the Python agent API's
// _require_canvas_access_sync / _require_canvas_owner_sync decorators
// (api/apps/restful_apis/agent_api.py:74-100). ErrAgentNotOwner is the
// owner-level sentinel used by DeleteAgent only.
func TestMapAgentError(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want common.ErrorCode
	}{
		{"nil", nil, common.CodeSuccess},
		{"user_canvas_not_found", dao.ErrUserCanvasNotFound, common.CodeOperatingError},
		{"user_canvas_version_not_found", dao.ErrUserCanvasVersionNotFound, common.CodeOperatingError},
		{"agent_not_owner", service.ErrAgentNotOwner, common.CodeOperatingError},
		{"generic", errors.New("boom"), common.CodeDataError},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ec, msg := mapAgentError(tc.err)
			if ec != tc.want {
				t.Errorf("mapAgentError(%v) = code %d, want %d", tc.err, ec, tc.want)
			}
			if tc.err == nil {
				if msg != "" {
					t.Errorf("mapAgentError(nil) = msg %q, want empty", msg)
				}
			} else if msg == "" {
				t.Errorf("mapAgentError(%v) returned empty message", tc.err)
			}
		})
	}
}

// --- PR3: smoke tests for the 13 new handlers ---

// TestAgentChatCompletions_RequiresAgentID covers the most important
// validation branch: missing agent_id -> 101 + "`agent_id` is required."
func TestAgentChatCompletions_RequiresAgentID(t *testing.T) {
	gin.SetMode(gin.TestMode)
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest("POST", "/api/v1/agents/chat/completions",
		strings.NewReader(`{"query":"hello","stream":false}`))
	c.Request.Header.Set("Content-Type", "application/json")
	c.Set("user", &entity.User{ID: "u1"})
	c.Set("user_id", "u1")

	h := NewAgentHandler(service.NewAgentService(), nil)
	h.AgentChatCompletions(c)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", w.Code)
	}
	var resp map[string]interface{}
	_ = json.Unmarshal(w.Body.Bytes(), &resp)
	if code, _ := resp["code"].(float64); code != float64(common.CodeArgumentError) {
		t.Errorf("code = %v, want 101", code)
	}
	if msg, _ := resp["message"].(string); !strings.Contains(msg, "`agent_id` is required.") {
		t.Errorf("message = %q, want to contain agent_id is required", msg)
	}
}

// TestAgentChatCompletions_OpenAICompat_EmptyMessages covers the
// openai-compatible 102 branch: empty messages list.
func TestAgentChatCompletions_OpenAICompat_EmptyMessages(t *testing.T) {
	gin.SetMode(gin.TestMode)
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest("POST", "/api/v1/agents/chat/completions",
		strings.NewReader(`{"agent_id":"a1","openai-compatible":true,"model":"m","messages":[]}`))
	c.Request.Header.Set("Content-Type", "application/json")
	c.Set("user", &entity.User{ID: "u1"})
	c.Set("user_id", "u1")

	h := NewAgentHandler(service.NewAgentService(), nil)
	h.AgentChatCompletions(c)

	var resp map[string]interface{}
	_ = json.Unmarshal(w.Body.Bytes(), &resp)
	if code, _ := resp["code"].(float64); code != float64(common.CodeDataError) {
		t.Errorf("code = %v, want 102", code)
	}
	if msg, _ := resp["message"].(string); !strings.Contains(msg, "at least one message") {
		t.Errorf("message = %q, want to contain 'at least one message'", msg)
	}
}

// TestAgentChatCompletions_StreamSetsContentType covers the SSE
// branch: Content-Type must be text/event-stream and the body must
// end with "data: [DONE]\\n\\n".

func TestRerunAgent_RequiresAllFields(t *testing.T) {
	gin.SetMode(gin.TestMode)
	cases := []struct {
		name    string
		body    string
		missing string
	}{
		{"empty", `{}`, "id,dsl,component_id"},
		{"only_id", `{"id":"x"}`, "dsl,component_id"},
		{"id_dsl", `{"id":"x","dsl":{}}`, "component_id"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			w := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(w)
			c.Request = httptest.NewRequest("POST", "/api/v1/agents/rerun",
				strings.NewReader(tc.body))
			c.Request.Header.Set("Content-Type", "application/json")
			c.Set("user", &entity.User{ID: "u1"})
			c.Set("user_id", "u1")

			h := NewAgentHandler(service.NewAgentService(), nil)
			h.RerunAgent(c)

			var resp map[string]interface{}
			_ = json.Unmarshal(w.Body.Bytes(), &resp)
			if code, _ := resp["code"].(float64); code != float64(common.CodeArgumentError) {
				t.Errorf("code = %v, want 101", code)
			}
			if msg, _ := resp["message"].(string); !strings.Contains(msg, "required argument are missing") {
				t.Errorf("message = %q, want to contain 'required argument are missing'", msg)
			}
		})
	}
}

// TestRerunAgent_AcceptsCompleteRequest covers the happy path: all
// three required fields present -> 200 / code 0.

func TestPromptsReturnsHardcodedFields(t *testing.T) {
	gin.SetMode(gin.TestMode)
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest("GET", "/api/v1/agents/prompts", nil)
	c.Set("user", &entity.User{ID: "u1"})
	c.Set("user_id", "u1")

	h := NewAgentHandler(service.NewAgentService(), nil)
	h.Prompts(c)

	var resp map[string]interface{}
	_ = json.Unmarshal(w.Body.Bytes(), &resp)
	if code, _ := resp["code"].(float64); code != float64(common.CodeSuccess) {
		t.Fatalf("code = %v, want 0", code)
	}
	data, _ := resp["data"].(map[string]interface{})
	for _, key := range []string{"task_analysis", "output_format", "citation_guidelines", "few_shots_examples"} {
		if _, ok := data[key]; !ok {
			t.Errorf("prompts.data should contain %q, got: %v", key, data)
		}
	}
}

// TestGetAgentWebhookLogsReturnsEmptyPoll covers the contract: the
// payload must be {events:[], finished:false, next_since_ts:0}. This
// test exercises the handler's auth + response-shape path against a
// real (in-memory) user_canvas row.

func TestAgentExecutionRejectsUnimplementedOperations(t *testing.T) {
	for _, test := range []struct {
		name, method, path, body string
		run                      func(*AgentHandler, *gin.Context)
	}{
		{"run", "POST", "/api/v1/agents/c1/run", "", (*AgentHandler).RunAgent},
		{"chat-stream", "POST", "/api/v1/agents/chat/completions", `{"agent_id":"c1","stream":true,"query":"hi"}`, (*AgentHandler).AgentChatCompletions},
		{"chat-openai", "POST", "/api/v1/agents/chat/completions", `{"agent_id":"c1","openai-compatible":true,"messages":[{"role":"user","content":"hi"}]}`, (*AgentHandler).AgentChatCompletions},
		{"rerun", "POST", "/api/v1/agents/rerun", `{"id":"c1","dsl":{},"component_id":"begin"}`, (*AgentHandler).RerunAgent},
		{"webhook", "GET", "/api/v1/agents/c1/webhook/logs", "", (*AgentHandler).GetAgentWebhookLogs},
	} {
		t.Run(test.name, func(t *testing.T) {
			c, w, db := setupGinContextWithUserAndDB(t, test.method, test.path)
			if err := db.Create(&entity.UserCanvas{ID: "c1", UserID: "user-1", Title: agentString("target")}).Error; err != nil {
				t.Fatal(err)
			}
			c.Params = gin.Params{{Key: "canvas_id", Value: "c1"}}
			c.Request = httptest.NewRequest(test.method, test.path, strings.NewReader(test.body))
			c.Request.Header.Set("Content-Type", "application/json")
			test.run(NewAgentHandler(service.NewAgentService(), nil), c)
			var body map[string]interface{}
			if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
				t.Fatal(err)
			}
			if body["code"] == float64(common.CodeSuccess) || !strings.Contains(body["message"].(string), "not implemented") {
				t.Fatalf("fake success: %s", w.Body)
			}
			if strings.Contains(w.Header().Get("Content-Type"), "event-stream") || strings.Contains(w.Body.String(), "hello") || strings.Contains(w.Body.String(), "[DONE]") {
				t.Fatal("synthetic output leaked", w.Body)
			}
			var sessions int64
			db.Model(&entity.API4Conversation{}).Count(&sessions)
			if sessions != 0 {
				t.Fatal("execution rejection created session")
			}
		})
	}
}

func agentString(value string) *string { return &value }

func TestAgentAuthorizationBeforeHandlers(t *testing.T) {
	for _, identity := range []any{nil, "invalid", (*entity.User)(nil), &entity.User{}} {
		for _, action := range []func(*AgentHandler, *gin.Context){(*AgentHandler).Prompts, (*AgentHandler).ListAgentTags, (*AgentHandler).RunAgent, (*AgentHandler).AgentChatCompletions, (*AgentHandler).CreateAgentSession} {
			recorder := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(recorder)
			c.Request = httptest.NewRequest("POST", "/", strings.NewReader(`{}`))
			if identity != nil {
				c.Set("user", identity)
			}
			action(NewAgentHandler(nil, nil), c)
			var response map[string]interface{}
			if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
				t.Fatal(err)
			}
			if response["code"] != float64(common.CodeUnauthorized) {
				t.Fatalf("invalid actor authorized: %T %s", identity, recorder.Body)
			}
		}
	}
}
