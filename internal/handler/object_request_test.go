package handler

import (
	"github.com/gin-gonic/gin"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestObjectRequestRejectsAmbiguousBodies(t *testing.T) {
	for _, body := range []string{`null`, `[]`, `{"ids":[],"ids":["foreign"]}`, `{} {}`, `{"models":[],"models":null}`, `{"ids":`, strings.Repeat(" ", (1<<20)+1)} {
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest("DELETE", "/", strings.NewReader(body))
		if _, err := objectRequest(c); err == nil {
			t.Fatalf("accepted ambiguous body of %d bytes", len(body))
		}
	}
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest("DELETE", "/", strings.NewReader(`{"ids":[]}`))
	fields, err := objectRequest(c)
	if err != nil || string(fields["ids"]) != "[]" {
		t.Fatal("empty selection lost", err)
	}
}
