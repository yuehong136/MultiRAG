package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"strconv"
	"strings"

	"multirag/internal/cli/contextengine"
)

// skillCoreClient preserves the upstream provider's /skills calls while binding
// them to an explicit protocol namespace. It never probes incompatible aliases.
type skillCoreClient struct {
	client *HTTPClient
	ctx    context.Context
}

func (a *skillCoreClient) Request(method, path string, _ bool, _ string, _ map[string]string, body map[string]interface{}) (*contextengine.HTTPResponse, error) {
	path = strings.Replace(path, "/skills", "/skill-core", 1)
	parsed, err := url.Parse(path)
	if err != nil {
		return nil, err
	}
	key := ""
	if method == "GET" && parsed.Query().Get("page") == "" {
		switch parsed.Path {
		case "/files":
			key = "files"
		case "/skill-core/spaces":
			key = "spaces"
		}
	}
	if key != "" {
		return a.listAll(parsed, key)
	}
	binary := method == "GET" && strings.HasPrefix(parsed.Path, "/files/")
	var reader io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		reader = bytes.NewReader(encoded)
	}
	expected := 200
	if method == "DELETE" && strings.HasPrefix(parsed.Path, "/skill-core/spaces/") {
		expected = 202
	}
	data, err := a.client.skillProtocolRequest(a.ctx, method, path, "application/json", "", reader, expected, binary)
	if err != nil {
		return nil, err
	}
	if !binary {
		data, err = json.Marshal(map[string]any{"code": 0, "data": json.RawMessage(data)})
		if err != nil {
			return nil, err
		}
	}
	return &contextengine.HTTPResponse{StatusCode: expected, Body: data}, nil
}

// Directory traversal must see every child; the upstream provider assumes an
// unpaginated directory and would otherwise lose files after the server limit.
func (a *skillCoreClient) listAll(parsed *url.URL, key string) (*contextengine.HTTPResponse, error) {
	rows := []json.RawMessage{}
	seen := map[string]bool{}
	total := -1
	for page := 1; ; page++ {
		query := parsed.Query()
		query.Set("page", strconv.Itoa(page))
		query.Set("page_size", "100")
		parsed.RawQuery = query.Encode()
		data, err := a.client.skillProtocolRequest(a.ctx, "GET", parsed.String(), "", "", nil, 200, false)
		if err != nil {
			return nil, err
		}
		var result map[string]json.RawMessage
		if err = json.Unmarshal(data, &result); err != nil {
			return nil, err
		}
		var batch []json.RawMessage
		var count int
		if err = json.Unmarshal(result[key], &batch); err != nil {
			return nil, fmt.Errorf("invalid %s listing", key)
		}
		if err = json.Unmarshal(result["total"], &count); err != nil || count < 0 {
			return nil, fmt.Errorf("missing listing total")
		}
		if total >= 0 && count != total {
			return nil, fmt.Errorf("directory changed during traversal; retry")
		}
		total = count
		for _, row := range batch {
			var item struct {
				ID string `json:"id"`
			}
			if json.Unmarshal(row, &item) != nil || item.ID == "" || seen[item.ID] {
				return nil, fmt.Errorf("invalid or repeating listing")
			}
			seen[item.ID] = true
			rows = append(rows, row)
		}
		if len(rows) == total {
			break
		}
		if len(rows) > total || len(batch) == 0 || len(rows) > 100000 {
			return nil, fmt.Errorf("incomplete listing; refusing truncated traversal")
		}
	}
	encoded, err := json.Marshal(map[string]any{"code": 0, "data": map[string]any{key: rows, "total": total}})
	return &contextengine.HTTPResponse{StatusCode: 200, Body: encoded}, err
}

func (a *skillCoreClient) UploadMultipart(path, contentType string, body io.Reader) error {
	_, err := a.client.skillProtocolRequest(a.ctx, "POST", path, contentType, "", body, 200, false)
	return err
}
