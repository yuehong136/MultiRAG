package cli

import (
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"testing"
)

func TestDatasetDocumentNameResolution(t *testing.T) {
	rows := []map[string]any{{"id": "9e235004-bd89-11f1-9fca-acde48001122", "name": "knowledge name"}}
	srv := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/datasets" {
			t.Error("name query lost")
		}
		json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": rows, "total_datasets": len(rows)})
	}))
	defer srv.Close()
	u, _ := url.Parse(srv.URL)
	host, port, _ := net.SplitHostPort(u.Host)
	n, _ := strconv.Atoi(port)
	c := newConsumerCLI("user")
	consumerHTTP(c).Host = host
	consumerHTTP(c).Port = n
	if id, err := c.getDatasetID("knowledge name"); err != nil || id != "9e235004-bd89-11f1-9fca-acde48001122" {
		t.Fatal(id, err)
	}
	rows = append(rows, map[string]any{"id": "9e235005-bd89-11f1-9fca-acde48001122", "name": "knowledge name"})
	if _, err := c.getDatasetID("knowledge name"); err == nil {
		t.Fatal("ambiguous dataset name accepted")
	}
}

func TestDatasetDocumentClientErrorReturnsNilInterface(t *testing.T) {
	srv := providerKeyServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"code":500,"message":"invalid document metadata","data":null}`)
	}))
	defer srv.Close()
	u, _ := url.Parse(srv.URL)
	host, port, _ := net.SplitHostPort(u.Host)
	number, _ := strconv.Atoi(port)
	c := newConsumerCLI("user")
	consumerHTTP(c).Host = host
	consumerHTTP(c).Port = number
	consumerHTTP(c).LoginToken = consumerTokenPtr("controlled")
	cmd := NewCommand("list_dataset_documents")
	cmd.Params["dataset"] = "9e235004bd8911f19fcaacde48001122"
	result, err := c.ListDatasetDocumentUserCommand(cmd)
	if err == nil || result != nil {
		t.Fatal("failed command returned a typed nil response", result, err)
	}
}
