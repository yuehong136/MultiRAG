package handler

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/dao"
	"multirag/internal/service"
)

func TestFileAncestryErrorClassification(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, tc := range []struct {
		name         string
		err          error
		status, code int
	}{
		{"denied", service.ErrFileNoAuthorization, http.StatusOK, 102},
		{"wrapped denial", fmt.Errorf("private query context: %w", service.ErrFileNoAuthorization), http.StatusOK, 102},
		{"managed tree", dao.ErrSkillManagedFile, http.StatusNotFound, 404},
		{"other failure", errors.New("test failure"), http.StatusOK, 500},
	} {
		t.Run(tc.name, func(t *testing.T) {
			w := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(w)
			respondFileAncestryError(c, tc.err)
			var body struct {
				Code int
				Data any
			}
			if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
				t.Fatal(err)
			}
			if w.Code != tc.status || body.Code != tc.code || body.Data != nil || strings.Contains(w.Body.String(), "private query context") {
				t.Fatalf("unexpected ancestry error: %s", w.Body)
			}
		})
	}
}
