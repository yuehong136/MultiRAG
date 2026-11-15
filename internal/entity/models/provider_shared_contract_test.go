package models

import (
	"net/http"
)

type providerContractTransport func(*http.Request) (*http.Response, error)

func (f providerContractTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
