package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"mime"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"multirag/internal/common"
	"multirag/internal/entity"
	"multirag/internal/service"
)

type documentFileCall struct {
	kind, user, document, dataset, filename, run string
	download                                     bool
	session                                      *string
}
type fakeDocumentFileReader struct {
	file  *service.DocumentFile
	err   error
	calls []documentFileCall
}

func (f *fakeDocumentFileReader) GetDocumentPreview(_ context.Context, user, doc string, download bool) (*service.DocumentFile, error) {
	f.calls = append(f.calls, documentFileCall{kind: "preview", user: user, document: doc, download: download})
	return f.file, f.err
}
func (f *fakeDocumentFileReader) DownloadDocument(_ context.Context, tenant, dataset, doc string) (*service.DocumentFile, error) {
	f.calls = append(f.calls, documentFileCall{kind: "sdk", user: tenant, dataset: dataset, document: doc})
	return f.file, f.err
}
func (f *fakeDocumentFileReader) GetDocumentArtifact(_ context.Context, user, filename, run string, session *string) (*service.DocumentFile, error) {
	f.calls = append(f.calls, documentFileCall{kind: "artifact", user: user, filename: filename, run: run, session: session})
	return f.file, f.err
}
func documentFilesRouter(f *fakeDocumentFileReader, identity interface{}, tenant string) *gin.Engine {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(func(c *gin.Context) {
		if identity != nil {
			c.Set("user", identity)
		}
		if tenant != "" {
			c.Set("document_token_tenant", tenant)
		}
	})
	h := &DocumentHandler{fileService: f}
	r.GET("/documents/:id/preview", h.GetDocumentPreview)
	r.GET("/documents/:id/download", h.DownloadDocumentFile)
	r.GET("/documents/artifact/:filename", h.GetDocumentArtifact)
	r.GET("/datasets/:dataset_id/documents/:document_id", h.DownloadDocument)
	return r
}
func documentFilesGET(r http.Handler, path string) *httptest.ResponseRecorder {
	response := httptest.NewRecorder()
	r.ServeHTTP(response, httptest.NewRequest(http.MethodGet, path, nil))
	return response
}
func assertDocumentFilePrivate(t *testing.T, response *httptest.ResponseRecorder) {
	t.Helper()
	if response.Header().Get("Cache-Control") != "private, no-store" || response.Header().Get("X-Content-Type-Options") != "nosniff" {
		t.Fatalf("missing private binary response headers: %v", response.Header())
	}
}
func TestDocumentFilesRefuseIdentityBeforeService(t *testing.T) {
	for _, identity := range []interface{}{nil, "untrusted", (*entity.User)(nil), &entity.User{}} {
		for _, path := range []string{"/documents/doc/preview", "/documents/doc/download", "/documents/artifact/file.json?run_id=run"} {
			f := &fakeDocumentFileReader{}
			response := documentFilesGET(documentFilesRouter(f, identity, ""), path)
			assertDocumentFilePrivate(t, response)
			if response.Code != http.StatusUnauthorized || len(f.calls) != 0 {
				t.Fatalf("identity=%v path=%s status=%d calls=%v", identity, path, response.Code, f.calls)
			}
		}
	}
}
func TestDocumentFilesForwardUserAndDownloadPolicy(t *testing.T) {
	for _, download := range []bool{false, true} {
		path := "/documents/doc-1/preview"
		if download {
			path = "/documents/doc-1/download"
		}
		f := &fakeDocumentFileReader{file: &service.DocumentFile{Data: []byte("content"), FileName: "report.pdf", ContentType: "application/pdf"}}
		response := documentFilesGET(documentFilesRouter(f, &entity.User{ID: "member"}, ""), path)
		if response.Code != http.StatusOK || response.Body.String() != "content" || len(f.calls) != 1 || f.calls[0].user != "member" || f.calls[0].document != "doc-1" || f.calls[0].download != download {
			t.Fatalf("status=%d body=%s calls=%+v", response.Code, response.Body.String(), f.calls)
		}
		prefix := "inline;"
		if download {
			prefix = "attachment;"
		}
		if !strings.HasPrefix(response.Header().Get("Content-Disposition"), prefix) {
			t.Fatal(response.Header())
		}
		assertDocumentFilePrivate(t, response)
	}
}
func TestDocumentFilesErrorsAreOpaque(t *testing.T) {
	for _, tc := range []struct {
		err    error
		status int
		code   common.ErrorCode
	}{
		{service.ErrDocumentFileUnavailable, http.StatusNotFound, common.CodeDataError},
		{service.ErrDocumentDownloadDenied, http.StatusForbidden, common.CodeForbidden},
		{errors.New("storage secret backend path"), http.StatusInternalServerError, common.CodeExceptionError},
	} {
		f := &fakeDocumentFileReader{err: tc.err}
		response := documentFilesGET(documentFilesRouter(f, &entity.User{ID: "member"}, ""), "/documents/doc/download")
		var envelope struct {
			Code common.ErrorCode `json:"code"`
			Data json.RawMessage  `json:"data"`
		}
		if err := json.Unmarshal(response.Body.Bytes(), &envelope); err != nil {
			t.Fatal(err)
		}
		if response.Code != tc.status || envelope.Code != tc.code || string(envelope.Data) != "null" || strings.Contains(response.Body.String(), "secret") {
			t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
		}
		assertDocumentFilePrivate(t, response)
	}
	f := &fakeDocumentFileReader{}
	response := documentFilesGET(documentFilesRouter(f, &entity.User{ID: "member"}, ""), "/documents/doc/preview")
	if response.Code != http.StatusInternalServerError {
		t.Fatalf("nil file accepted: %d %s", response.Code, response.Body.String())
	}
}
func TestDocumentFilesPreserveEmptyAndJSONSource(t *testing.T) {
	for _, data := range [][]byte{{}, []byte(`{"code":109,"message":"source data","data":null}`)} {
		f := &fakeDocumentFileReader{file: &service.DocumentFile{Data: data, FileName: "source.json", ContentType: "application/json"}}
		response := documentFilesGET(documentFilesRouter(f, &entity.User{ID: "member"}, ""), "/documents/doc/download")
		if response.Code != http.StatusOK || !bytes.Equal(response.Body.Bytes(), data) || response.Header().Get("Content-Type") != "application/json" {
			t.Fatalf("source changed: %d %s", response.Code, response.Body.String())
		}
		assertDocumentFilePrivate(t, response)
	}
}
func TestDocumentFilesSafeDisposition(t *testing.T) {
	for _, tc := range []struct {
		name, mime string
		attachment bool
	}{
		{"展示 空格\"\r\nX-Evil: true.pdf", "application/pdf", false},
		{"picture.SVG", "image/svg+xml", true},
		{"page.html", "text/html", true},
		{"source.txt", "application/xhtml+xml", true},
		{"source.txt", "Text/HTML; charset=utf-8", true},
	} {
		f := &fakeDocumentFileReader{file: &service.DocumentFile{Data: []byte("data"), FileName: tc.name, ContentType: tc.mime}}
		response := documentFilesGET(documentFilesRouter(f, &entity.User{ID: "member"}, ""), "/documents/doc/preview")
		value := response.Header().Get("Content-Disposition")
		if response.Code != http.StatusOK || !strings.Contains(value, "filename*=UTF-8''") || strings.ContainsAny(value, "\r\n") || response.Header().Get("X-Evil") != "" {
			t.Fatalf("unsafe header: %v", response.Header())
		}
		disposition, params, err := mime.ParseMediaType(value)
		if err != nil || params["filename"] != tc.name {
			t.Fatalf("original filename was not preserved: header=%s parsed=%v err=%v", value, params, err)
		}
		_, fallback, found := strings.Cut(value, `; filename="`)
		fallback, _, closed := strings.Cut(fallback, `"`)
		if !found || !closed || fallback == "" || strings.ContainsAny(fallback, "\\/\r\n") || strings.ContainsFunc(fallback, func(r rune) bool { return r < 32 || r >= 127 }) {
			t.Fatalf("unsafe or missing quoted ASCII filename: %s", value)
		}
		wantDisposition := "inline"
		if tc.attachment {
			wantDisposition = "attachment"
		}
		if disposition != wantDisposition {
			t.Fatalf("wrong policy for %+v: %s", tc, value)
		}
		if strings.HasPrefix(tc.name, "展示") && (!strings.Contains(value, "%E5%B1%95") || !strings.Contains(value, "%20")) {
			t.Fatalf("filename lost UTF8 encoding: %s", value)
		}
	}
}
func TestDocumentArtifactPreservesJSONAndQuotedFilename(t *testing.T) {
	const filename = "0123456789abcdef0123456789abcdef.json"
	const runID = "fedcba9876543210fedcba9876543210"
	data := []byte(`{"code":109,"message":"artifact source","data":null}`)
	f := &fakeDocumentFileReader{file: &service.DocumentFile{Data: data, FileName: filename, ContentType: "application/json"}}
	response := documentFilesGET(documentFilesRouter(f, &entity.User{ID: "owner"}, ""), "/documents/artifact/"+filename+"?run_id="+runID)
	if response.Code != http.StatusOK || !bytes.Equal(response.Body.Bytes(), data) || response.Header().Get("Content-Type") != "application/json" {
		t.Fatalf("artifact source changed: status=%d headers=%v body=%s", response.Code, response.Header(), response.Body.String())
	}
	value := response.Header().Get("Content-Disposition")
	if !strings.Contains(value, `filename="`+filename+`"`) {
		t.Fatalf("Web artifact consumer requires the quoted filename: %s", value)
	}
	disposition, params, err := mime.ParseMediaType(value)
	if err != nil || disposition != "inline" || params["filename"] != filename {
		t.Fatalf("invalid artifact disposition: %s err=%v", value, err)
	}
	if len(f.calls) != 1 || f.calls[0].filename != filename || f.calls[0].run != runID || f.calls[0].user != "owner" || f.calls[0].session != nil {
		t.Fatalf("artifact scope changed: %+v", f.calls)
	}
	assertDocumentFilePrivate(t, response)
}
func TestDocumentArtifactRejectsMalformedAndDuplicateScope(t *testing.T) {
	for _, query := range []string{"", "run_id=one&run_id=two", "run_id=one&session_id=a&session_id=b", "run_id=%xx", "run_id=one;session_id=x"} {
		f := &fakeDocumentFileReader{}
		response := documentFilesGET(documentFilesRouter(f, &entity.User{ID: "owner"}, ""), "/documents/artifact/file.json?"+query)
		if response.Code != http.StatusNotFound || len(f.calls) != 0 {
			t.Fatalf("query=%s status=%d calls=%+v", query, response.Code, f.calls)
		}
		assertDocumentFilePrivate(t, response)
	}
}
func TestDocumentArtifactForwardsExactScope(t *testing.T) {
	for _, query := range []string{"run_id=run-1", "run_id=run-1&session_id=", "run_id=run-1&session_id=session-1"} {
		f := &fakeDocumentFileReader{file: &service.DocumentFile{Data: []byte("artifact"), FileName: "file.json", ContentType: "application/json"}}
		response := documentFilesGET(documentFilesRouter(f, &entity.User{ID: "owner"}, ""), "/documents/artifact/file.json?"+query)
		if response.Code != http.StatusOK || len(f.calls) != 1 {
			t.Fatalf("status=%d calls=%+v", response.Code, f.calls)
		}
		call := f.calls[0]
		if call.user != "owner" || call.filename != "file.json" || call.run != "run-1" {
			t.Fatalf("wrong scope: %+v", call)
		}
		if !strings.Contains(query, "session_id=") {
			if call.session != nil {
				t.Fatal("omitted session became explicit")
			}
		} else {
			want := ""
			if strings.HasSuffix(query, "session-1") {
				want = "session-1"
			}
			if call.session == nil || *call.session != want {
				t.Fatalf("explicit session changed: %+v", call.session)
			}
		}
	}
}
func TestDocumentSDKRequiresAndForwardsTokenTenant(t *testing.T) {
	for _, tenant := range []string{"", "token-tenant"} {
		f := &fakeDocumentFileReader{file: &service.DocumentFile{Data: []byte("sdk"), FileName: "doc.pdf", ContentType: "application/pdf"}}
		response := documentFilesGET(documentFilesRouter(f, &entity.User{ID: "different-user"}, tenant), "/datasets/dataset-1/documents/doc-1")
		assertDocumentFilePrivate(t, response)
		if tenant == "" {
			if response.Code != http.StatusUnauthorized || len(f.calls) != 0 {
				t.Fatalf("user login bypassed SDK auth: %d %+v", response.Code, f.calls)
			}
		} else {
			if response.Code != http.StatusOK || len(f.calls) != 1 || f.calls[0].user != tenant || f.calls[0].dataset != "dataset-1" || f.calls[0].document != "doc-1" {
				t.Fatalf("tenant binding changed: %d %+v", response.Code, f.calls)
			}
		}
	}
}
func TestDatasetDocumentAuthRejectsMalformedCredentials(t *testing.T) {
	t.Setenv("DISABLE_SDK", "")
	for _, headers := range [][]string{nil, {"token"}, {"Basic token"}, {"Bearer"}, {"Bearer "}, {"Bearer token extra"}, {"Bearer token,other"}, {"Bearer token", "Bearer other"}, {"Bearer\ttoken"}, {" Bearer token"}, {"Bearer  token"}} {
		gin.SetMode(gin.TestMode)
		r := gin.New()
		called := false
		r.GET("/sdk", (&AuthHandler{}).DatasetDocumentAuthMiddleware(), func(c *gin.Context) { called = true; c.Status(http.StatusOK) })
		request := httptest.NewRequest(http.MethodGet, "/sdk", nil)
		for _, header := range headers {
			request.Header.Add("Authorization", header)
		}
		response := httptest.NewRecorder()
		r.ServeHTTP(response, request)
		if response.Code != http.StatusUnauthorized || called {
			t.Fatalf("headers=%v status=%d handler=%v", headers, response.Code, called)
		}
		assertDocumentFilePrivate(t, response)
	}
}
func TestDatasetDocumentAuthRespectsDisabledSDK(t *testing.T) {
	t.Setenv("DISABLE_SDK", "1")
	gin.SetMode(gin.TestMode)
	r := gin.New()
	called := false
	r.GET("/sdk", (&AuthHandler{}).DatasetDocumentAuthMiddleware(), func(c *gin.Context) { called = true })
	request := httptest.NewRequest(http.MethodGet, "/sdk", nil)
	request.Header.Set("Authorization", "Bearer valid-key")
	response := httptest.NewRecorder()
	r.ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized || called {
		t.Fatalf("SDK disable bypassed: %d", response.Code)
	}
	assertDocumentFilePrivate(t, response)
}
