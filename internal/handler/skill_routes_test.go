package handler

import (
	"github.com/gin-gonic/gin"
	"multirag/internal/server"
	"net/http/httptest"
	"testing"
)

func TestSkillProtocolSelectionRejectsUnknown(t *testing.T) {
	t.Setenv("SKILLS_API_PROTOCOL", "unknown")
	if _, e := server.SkillsProtocol(); e == nil {
		t.Fatal("unknown protocol accepted")
	}
}
func TestSkillUnavailableCorePreservesAssetsStartup(t *testing.T) {
	t.Setenv("SKILLS_API_PROTOCOL", server.SkillsAssetsProtocol)
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	stop, e := AttachSkillCore(r, &server.Config{}, nil, nil, nil)
	if e != nil {
		t.Fatal(e)
	}
	defer stop()
	response := httptest.NewRecorder()
	r.ServeHTTP(response, httptest.NewRequest("GET", "/api/v1/skill-core/spaces", nil))
	if response.Code != 503 {
		t.Fatalf("unsupported core returned %d", response.Code)
	}
	t.Setenv("SKILLS_API_PROTOCOL", server.SkillsCoreProtocol)
	if _, e := AttachSkillCore(gin.New(), &server.Config{}, nil, nil, nil); e == nil {
		t.Fatal("explicit unsupported core startup succeeded")
	}
}
