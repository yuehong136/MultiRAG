package handler

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/service"

	"github.com/gin-gonic/gin"
)

func pluginToolsResponse(t *testing.T, user interface{}, setUser bool) *httptest.ResponseRecorder {
	t.Helper()
	gin.SetMode(gin.TestMode)
	r := gin.New()
	h := NewPluginHandler(service.NewPluginService())
	r.GET("/api/v1/plugin/tools", func(c *gin.Context) {
		if setUser {
			c.Set("user", user)
		}
		h.ListLLMTools(c)
	})
	response := httptest.NewRecorder()
	r.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/api/v1/plugin/tools", nil))
	return response
}

func TestPluginHandlerListLLMTools(t *testing.T) {
	response := pluginToolsResponse(t, &entity.User{ID: "user-1"}, true)
	var envelope struct {
		Code    common.ErrorCode          `json:"code"`
		Message string                    `json:"message"`
		Data    []service.LLMToolMetadata `json:"data"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &envelope); err != nil {
		t.Fatal(err)
	}
	if response.Code != http.StatusOK || envelope.Code != common.CodeSuccess || envelope.Message != "success" {
		t.Fatalf("unexpected response: status=%d body=%s", response.Code, response.Body.String())
	}
	if len(envelope.Data) != 1 || envelope.Data[0].Name != "bad_calculator" || len(envelope.Data[0].Parameters) != 2 {
		t.Fatalf("listing exposed tools outside the embedded snapshot: %+v", envelope.Data)
	}
	var raw struct {
		Data []map[string]json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &raw); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"name", "displayName", "description", "displayDescription", "parameters"} {
		if raw.Data[0][name] == nil {
			t.Fatalf("missing Python metadata key %s: %s", name, response.Body.String())
		}
	}
	var parameters map[string]map[string]json.RawMessage
	if err := json.Unmarshal(raw.Data[0]["parameters"], &parameters); err != nil {
		t.Fatal(err)
	}
	for name, parameter := range parameters {
		if len(parameter) != 4 || parameter["type"] == nil || parameter["description"] == nil || parameter["displayDescription"] == nil || parameter["required"] == nil {
			t.Fatalf("parameter %s has wrong metadata fields: %v", name, parameter)
		}
	}
}

func TestPluginHandlerListLLMToolsRejectsInvalidIdentity(t *testing.T) {
	for _, tc := range []struct {
		name string
		user interface{}
		set  bool
	}{
		{"absent", nil, false}, {"wrong type", "user-1", true}, {"typed nil", (*entity.User)(nil), true}, {"empty ID", &entity.User{}, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			response := pluginToolsResponse(t, tc.user, tc.set)
			var envelope struct {
				Code common.ErrorCode `json:"code"`
				Data json.RawMessage  `json:"data"`
			}
			if err := json.Unmarshal(response.Body.Bytes(), &envelope); err != nil {
				t.Fatal(err)
			}
			if envelope.Code != common.CodeUnauthorized || string(envelope.Data) != "null" {
				t.Fatalf("invalid identity received metadata: %s", response.Body.String())
			}
		})
	}
}
