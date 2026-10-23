package contextengine

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

type datasetSearchHTTP struct{ base string }

func (c datasetSearchHTTP) Request(method, path, authKind string, headers map[string]string, body map[string]interface{}) (*HTTPResponse, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequest(method, c.base+"/api/v1"+path, bytes.NewReader(raw))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer controlled-session")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	return &HTTPResponse{StatusCode: resp.StatusCode, Body: data}, err
}

func TestDatasetProviderSearchRESTHTTP(t *testing.T) {
	for _, path := range []string{"", "knowledge"} {
		t.Run(path, func(t *testing.T) {
			searches := 0
			api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer controlled-session" {
					t.Error("session lost")
				}
				switch r.URL.Path {
				case "/api/v1/datasets":
					io.WriteString(w, `{"code":0,"data":[{"id":"selected","name":"knowledge"}]}`)
				case "/api/v1/datasets/search":
					searches++
					var body map[string]any
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
					}
					if r.Method != "POST" || !reflect.DeepEqual(body["dataset_ids"], []any{"selected"}) || body["kb_id"] != nil || body["kb_ids"] != nil || body["question"] != "availability" {
						t.Error("wrong retrieval contract", body)
					}
					io.WriteString(w, `{"code":0,"data":{"chunks":[{"chunk_id":"chunk","doc_id":"doc","kb_id":"selected","content_with_weight":"answer"}],"total":1}}`)
				default:
					t.Error("obsolete path", r.URL.Path)
					http.NotFound(w, r)
				}
			}))
			defer api.Close()
			p := NewDatasetProvider(datasetSearchHTTP{base: api.URL})
			result, err := p.Search(context.Background(), path, &SearchOptions{Query: "availability"})
			if err != nil || searches != 1 || len(result.Nodes) != 1 {
				t.Fatalf("result=%v error=%v searches=%d", result, err, searches)
			}
		})
	}
}
