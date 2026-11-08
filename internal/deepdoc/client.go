// Package deepdoc implements the optional DLA multipart wire contract.
// It does not provide a local ONNX OCR/TSR implementation.
package deepdoc

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

var ErrNoURL = errors.New("deepdoc: DLA URL not configured")
var ErrNoRemoteEndpoint = errors.New("deepdoc: no verified remote OCR/TSR endpoint; local ONNX implementation required")
var ErrInvalidResponse = errors.New("deepdoc: invalid response")

const DefaultPerAttemptTimeout = 18 * time.Second
const DefaultMaxAttempts = 3
const DefaultBackoff = 200 * time.Millisecond
const MaxBackoff = 3 * time.Second
const maxResponseBytes = 4 << 20

type Client struct {
	baseURL     string
	httpClient  *http.Client
	maxAttempts int
	backoff     time.Duration
	configErr   error
}
type Option func(*Client)

func WithHTTPClient(h *http.Client) Option { return func(c *Client) { c.httpClient = h } }
func WithMaxAttempts(n int) Option         { return func(c *Client) { c.maxAttempts = n } }
func WithBackoff(d time.Duration) Option   { return func(c *Client) { c.backoff = d } }
func NewClient(options ...Option) *Client {
	address := os.Getenv("DEEPDOC_URL")
	if address == "" {
		address = os.Getenv("TENSORRT_DLA_SVR")
	}
	return NewClientWithURL(address, options...)
}
func NewClientWithURL(address string, options ...Option) *Client {
	c := &Client{baseURL: strings.TrimRight(address, "/"), maxAttempts: DefaultMaxAttempts, backoff: DefaultBackoff}
	for _, option := range options {
		option(c)
	}
	if c.maxAttempts < 1 || c.maxAttempts > DefaultMaxAttempts || c.backoff < 0 || c.backoff > MaxBackoff {
		c.configErr = fmt.Errorf("deepdoc: invalid retry configuration")
	}
	if address != "" {
		u, err := url.Parse(address)
		if err != nil || u.Host == "" || u.Scheme != "http" && u.Scheme != "https" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
			c.configErr = fmt.Errorf("deepdoc: invalid DLA URL")
		}
	}
	if c.httpClient == nil {
		c.httpClient = &http.Client{}
	}
	// An injected transport is borrowed; leave it untouched. Redirects are not
	// part of the protocol, and should not disclose document bytes elsewhere.
	clone := *c.httpClient
	clone.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	c.httpClient = &clone
	return c
}
func (c *Client) Enabled() bool { return c != nil && c.baseURL != "" && c.configErr == nil }

type bodyBuilder func() (io.Reader, string)

func (c *Client) doPost(ctx context.Context, build bodyBuilder, validate func([]byte) error) ([]byte, error) {
	if c == nil || c.baseURL == "" {
		return nil, ErrNoURL
	}
	if c.configErr != nil {
		return nil, c.configErr
	}
	var last error
	backoff := c.backoff
	for attempt := 0; attempt < c.maxAttempts; attempt++ {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		requestCtx, cancel := context.WithTimeout(ctx, DefaultPerAttemptTimeout)
		body, contentType := build()
		req, err := http.NewRequestWithContext(requestCtx, http.MethodPost, c.baseURL+"/predict", body)
		if err != nil {
			cancel()
			return nil, err
		}
		req.Header.Set("Content-Type", contentType)
		response, err := c.httpClient.Do(req)
		retry := true
		if err != nil {
			last = fmt.Errorf("deepdoc: DLA request failed: %w", err)
		} else {
			data, readErr := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes+1))
			response.Body.Close()
			switch {
			case readErr != nil:
				last = readErr
			case len(data) > maxResponseBytes:
				last = fmt.Errorf("%w: response exceeds limit", ErrInvalidResponse)
				retry = false
			case response.StatusCode != http.StatusOK:
				last = fmt.Errorf("deepdoc: DLA HTTP %d", response.StatusCode)
				retry = response.StatusCode >= 500
			default:
				last = validate(data)
				if last == nil {
					cancel()
					return data, nil
				}
			}
		}
		cancel()
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if !retry {
			return nil, last
		}
		if attempt+1 < c.maxAttempts {
			timer := time.NewTimer(backoff)
			select {
			case <-ctx.Done():
				timer.Stop()
				return nil, ctx.Err()
			case <-timer.C:
			}
			backoff = min(backoff*2, MaxBackoff)
		}
	}
	return nil, last
}
