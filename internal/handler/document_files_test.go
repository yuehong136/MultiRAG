package handler

import (
	"bytes"
	"errors"
	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/service"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type canonicalArtifactService struct {
	fakeDocumentService
	result  *service.ArtifactResponse
	failure error
}

func (s *canonicalArtifactService) GetDocumentArtifact(string) (*service.ArtifactResponse, error) {
	return s.result, s.failure
}
func TestCanonicalArtifactRawBytesAndSafeHeaders(t *testing.T) {
	for _, data := range [][]byte{{}, []byte(`{"code":109,"data":null}`)} {
		fake := &canonicalArtifactService{result: &service.ArtifactResponse{Data: data, ContentType: "application/json", SafeFilename: "展示 \"\r\nX-Evil: value.json"}}
		h := NewDocumentHandler(fake, nil)
		w := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(w)
		c.Params = gin.Params{{Key: "filename", Value: "safe.json"}}
		h.GetDocumentArtifact(c)
		if w.Code != 200 || !bytes.Equal(w.Body.Bytes(), data) || w.Header().Get("Content-Type") != "application/json" || strings.ContainsAny(w.Header().Get("Content-Disposition"), "\r\n") || w.Header().Get("X-Evil") != "" {
			t.Fatal(w.Code, w.Header(), w.Body.String())
		}
	}
}
func TestCanonicalArtifactStorageErrorsAreOpaque(t *testing.T) {
	for _, fake := range []*canonicalArtifactService{{failure: errors.New("secret backend path")}, {}} {
		h := NewDocumentHandler(fake, nil)
		w := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(w)
		c.Params = gin.Params{{Key: "filename", Value: "safe.json"}}
		h.GetDocumentArtifact(c)
		if strings.Contains(w.Body.String(), "secret") || !strings.Contains(w.Body.String(), "Artifact not available") {
			t.Fatal(w.Body.String())
		}
	}
}
func TestCanonicalDocumentReadsRejectInvalidIdentity(t *testing.T) {
	for _, identity := range []any{nil, "untrusted", (*entity.User)(nil), &entity.User{}} {
		for _, preview := range []bool{true, false} {
			w := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(w)
			c.Request = httptest.NewRequest(http.MethodGet, "/documents/doc", nil)
			if identity != nil {
				c.Set("user", identity)
			}
			h := NewDocumentHandler(&fakeDocumentService{}, nil)
			if preview {
				h.GetDocumentPreview(c)
			} else {
				h.DownloadDocument(c)
			}
			if !strings.Contains(w.Body.String(), "\"code\"") || strings.Contains(w.Body.String(), "\"code\":0") {
				t.Fatal(identity, w.Body.String())
			}
		}
	}
	_ = common.CodeSuccess
}
