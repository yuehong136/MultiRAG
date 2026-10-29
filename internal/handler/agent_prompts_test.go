package handler

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/entity"
)

func promptHTTP(h *AgentHandler, identity any) *httptest.ResponseRecorder {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(func(c *gin.Context) {
		if identity != nil {
			c.Set("user", identity)
		}
	})
	r.GET("/api/v1/agents/prompts", h.GetPrompts)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/agents/prompts", nil))
	return w
}

func TestAgentPromptsRequiresActorBeforeLoading(t *testing.T) {
	for _, identity := range []any{nil, "user", (*entity.User)(nil), &entity.User{}} {
		calls := 0
		h := &AgentHandler{loadPrompt: func(string) (string, error) { calls++; return "", nil }}
		w := promptHTTP(h, identity)
		var response struct {
			Code common.ErrorCode `json:"code"`
		}
		if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil {
			t.Fatal(err)
		}
		if response.Code != common.CodeUnauthorized || calls != 0 {
			t.Fatalf("unauthorized load: %s calls=%d", w.Body, calls)
		}
	}
}

func TestAgentPromptsReturnsExactFiles(t *testing.T) {
	expected := map[string]string{}
	for _, name := range []string{"analyze_task_system", "analyze_task_user", "next_step", "reflect", "citation_prompt"} {
		content, err := os.ReadFile(filepath.Join("..", "..", "core", "prompts", name+".md"))
		if err != nil {
			t.Fatal(err)
		}
		expected[name] = strings.TrimSpace(string(content))
	}
	w := promptHTTP(NewAgentHandler(nil), &entity.User{ID: "actor"})
	var response struct {
		Code int               `json:"code"`
		Data map[string]string `json:"data"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	want := map[string]string{"task_analysis": expected["analyze_task_system"] + "\n\n" + expected["analyze_task_user"], "plan_generation": expected["next_step"], "reflection": expected["reflect"], "citation_guidelines": expected["citation_prompt"]}
	if w.Code != 200 || response.Code != 0 || !reflect.DeepEqual(response.Data, want) {
		t.Fatal("prompt response did not match complete source files")
	}
}

func TestAgentPromptsLoadingFailureIsOpaqueAndAtomic(t *testing.T) {
	for failAt := 0; failAt < 5; failAt++ {
		calls := 0
		h := &AgentHandler{loadPrompt: func(string) (string, error) {
			calls++
			if calls == failAt+1 {
				return "", errors.New("/private/path/credential")
			}
			return "private partial", nil
		}}
		w := promptHTTP(h, &entity.User{ID: "actor"})
		var response struct {
			Code int `json:"code"`
			Data any `json:"data"`
		}
		if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil {
			t.Fatal(err)
		}
		if response.Code == 0 || response.Data != nil || strings.Contains(w.Body.String(), "private") || calls != failAt+1 {
			t.Fatalf("partial/leaky success: %s", w.Body)
		}
	}
}
