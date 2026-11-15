package service

import (
	"multirag/internal/testsupport"
	"net/http"
	"net/http/httptest"
	"testing"
)

func registeredModelServer(t *testing.T, handler http.Handler) *httptest.Server {
	t.Helper()
	return testsupport.NewHTTP(t, handler)
}
