package cli

import (
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"testing"
)

func TestListDatasetDocumentsSyntaxAndEnvelope(t *testing.T) {
	if cmd, err := NewParser("LIST DOCUMENTS FROM 'knowledge name';").Parse(false); err != nil || cmd.Type != "list_dataset_documents" || cmd.Params["dataset"] != "knowledge name" {
		t.Fatal(cmd, err)
	}
	for _, input := range []string{"LIST DOCUMENTS 'x';", "LIST DOCUMENTS FROM '';", "LIST DOCUMENTS FROM 'x'; ignored", "LIST DOCUMENTS FROM 'x'"} {
		if _, err := NewParser(input).Parse(false); err == nil {
			t.Fatal("bad syntax accepted", input)
		}
	}
	for _, body := range []string{`{"data":{"total":0,"docs":[]}}`, `{"code":0,"retcode":1,"data":{"total":0,"docs":[]}}`, `{"code":0,"data":{"docs":[]}}`, `{"code":0,"data":{"total":0,"docs":null}}`, `{"code":0,"data":{"total":-1,"docs":[]}}`, `{"code":0,"data":{"total":2,"docs":[{"id":"a"},{"id":"a"}]}}`, `{"code":0,"data":{"total":1,"docs":[{}]}}`} {
		if _, err := decodeDatasetDocuments(&Response{StatusCode: 200, Body: []byte(body)}); err == nil {
			t.Fatal("bad document response accepted", body)
		}
	}
	r, err := decodeDatasetDocuments(&Response{StatusCode: 200, Body: []byte(`{"retcode":0,"data":{"total":0,"docs":[]}}`)})
	if err != nil || r.Total != 0 || r.Docs == nil {
		t.Fatal(r, err)
	}
}

func TestDatasetDocumentNameResolution(t *testing.T) {
	rows := []map[string]any{{"id": "9e235004-bd89-11f1-9fca-acde48001122", "name": "knowledge name"}}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/datasets" || r.URL.Query().Get("keywords") != "knowledge name" {
			t.Error("name query lost")
		}
		json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": rows, "total_datasets": len(rows)})
	}))
	defer srv.Close()
	u, _ := url.Parse(srv.URL)
	host, port, _ := net.SplitHostPort(u.Host)
	n, _ := strconv.Atoi(port)
	c := NewMultiRAGClient("user")
	c.HTTPClient.Host = host
	c.HTTPClient.Port = n
	if id, err := c.documentDatasetID("knowledge name"); err != nil || id != "9e235004bd8911f19fcaacde48001122" {
		t.Fatal(id, err)
	}
	rows = append(rows, map[string]any{"id": "9e235005-bd89-11f1-9fca-acde48001122", "name": "knowledge name"})
	if _, err := c.documentDatasetID("knowledge name"); err == nil {
		t.Fatal("ambiguous dataset name accepted")
	}
	for _, id := range []string{"9e235004-bd89-11f1-9fca-acde48001122", "9e235004bd8911f19fcaacde48001122"} {
		if _, err := c.documentDatasetID(id); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := c.documentDatasetID("9e235004-bd89-41f1-9fca-acde48001122"); err == nil {
		t.Fatal("non-UUID1 accepted")
	}
}
