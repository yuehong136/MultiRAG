package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

type skillsEnvelope struct {
	Code    *int            `json:"code"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data"`
}

func (c *HTTPClient) skillsRequest(ctx context.Context, method, path, contentType, key string, body io.Reader, expected int, binary bool) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, method, c.BuildURL("/skills"+path, true), body)
	if err != nil {
		return nil, err
	}
	for name, value := range c.Headers("api", nil) {
		req.Header.Set(name, value)
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	if key != "" {
		req.Header.Set("Idempotency-Key", key)
	}
	response, err := c.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("Skills request outcome is unknown; read back the resource or reuse the operation key: %w", err)
	}
	defer response.Body.Close()
	limit := int64(2 << 20)
	if binary && response.StatusCode == expected {
		limit = 64 << 20
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > limit {
		return nil, fmt.Errorf("Skills response exceeds size limit")
	}
	if binary && response.StatusCode == expected {
		return data, nil
	}
	var envelope skillsEnvelope
	if err := json.Unmarshal(data, &envelope); err != nil || envelope.Code == nil || len(envelope.Data) == 0 {
		return nil, fmt.Errorf("invalid Skills response (HTTP %d)", response.StatusCode)
	}
	if response.StatusCode != expected || *envelope.Code != 0 {
		var detail struct {
			ErrorCode string `json:"error_code"`
		}
		_ = json.Unmarshal(envelope.Data, &detail)
		return nil, fmt.Errorf("Skills %s (HTTP %d, code %d): %s", detail.ErrorCode, response.StatusCode, *envelope.Code, envelope.Message)
	}
	return envelope.Data, nil
}

func (c *HTTPClient) skillsJSON(ctx context.Context, method, path, key string, body any, expected int) ([]byte, error) {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		reader = bytes.NewReader(data)
	}
	return c.skillsRequest(ctx, method, path, "application/json", key, reader, expected, false)
}

func (c *HTTPClient) waitSkillOperation(ctx context.Context, id string) ([]byte, error) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		data, err := c.skillsJSON(ctx, "GET", "/operations/"+id, "", nil, 200)
		if err != nil {
			return nil, err
		}
		var payload struct {
			State string `json:"state"`
		}
		if json.Unmarshal(data, &payload) != nil {
			return nil, fmt.Errorf("invalid operation response")
		}
		switch payload.State {
		case "succeeded":
			return data, nil
		case "failed", "partial":
			return data, fmt.Errorf("Skills operation ended %s; inspect items before retrying", payload.State)
		case "pending", "running":
		default:
			return nil, fmt.Errorf("invalid operation state")
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-ticker.C:
		}
	}
}

// saveSkillDownload publishes only a complete response and never overwrites an existing path.
func saveSkillDownload(path string, data []byte) error {
	file, err := os.CreateTemp(filepath.Dir(path), ".skill-download-*")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	if _, err = file.Write(data); err != nil {
		file.Close()
		return err
	}
	if err = file.Close(); err != nil {
		return err
	}
	return os.Link(file.Name(), path)
}
