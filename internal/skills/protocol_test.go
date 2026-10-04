package skills

import (
	"github.com/gin-gonic/gin"
	"net/http/httptest"
	"testing"
)

func TestAssetsReadOnlyRejectsNewMutationsBeforeDatabase(t *testing.T) {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	s := New(nil, nil, nil, nil)
	s.RegisterAt(r, func() string { return "" }, "/api/v1/skill-assets", true)
	out := httptest.NewRecorder()
	r.ServeHTTP(out, httptest.NewRequest("POST", "/api/v1/skill-assets/spaces", nil))
	if out.Code != 503 {
		t.Fatalf("readonly mutation returned %d", out.Code)
	}
}
