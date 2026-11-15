package cli

import (
	"io"
	"net/http"
	"strings"
)

type loginFailureTransport struct {
	method string
	cause  error
}

func (s loginFailureTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	if request.Method == s.method {
		return nil, s.cause
	}
	return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader("pong")), Header: make(http.Header)}, nil
}
