package handler

import (
	"github.com/gin-gonic/gin"
	"multirag/internal/testsupport"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestAfter7fbObjectJSON(t *testing.T) {
	for _, raw := range []string{`{"name":"x"}`, `{}`, `[]`, `null`, `{"name":null}`, `{"name":"a","name":"b"}`, `{"unknown":"credential"}`, `{"name":3}`, `{"name":"x"} {}`} {
		t.Run(raw, func(t *testing.T) {
			testsupport.NewCase(t)
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest("POST", "/", strings.NewReader(raw))
			var req struct {
				Name *string `json:"name"`
			}
			err := bindObjectJSON(c, &req)
			valid := raw == `{"name":"x"}` || raw == `{}`
			if (err == nil) != valid {
				t.Fatal(err)
			}
		})
	}
}
