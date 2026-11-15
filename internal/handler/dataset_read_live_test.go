package handler

import (
	"multirag/internal/service"
	"testing"
)

func TestDatasetReadHTTPAndSQL(t *testing.T) {
	f := newCanonicalHTTPFixture(t)
	f.document(t, "shared", "one.txt")
	f.document(t, "shared", "two.txt")
	f.document(t, "private", "private.txt")
	f.document(t, "foreign", "foreign.txt")
	f.document(t, "disabled", "disabled.txt")
	h := NewDocumentHandler(service.NewDocumentService(), service.NewDatasetService())
	app := f.app()
	app.GET("/datasets/:dataset_id/documents", h.ListDocuments)
	url := f.serve(t, app)
	for _, actor := range []string{"owner", "member"} {
		status, result, _, _ := f.request(t, url, actor, "GET", "/datasets/"+f.datasets["shared"]+"/documents?page=2&page_size=1", nil)
		data, _ := result["data"].(map[string]any)
		docs, _ := data["docs"].([]any)
		if status != 200 || result["code"] != float64(0) || data["total"] != float64(2) || len(docs) != 1 {
			t.Fatal("canonical authorized pagination", actor, result)
		}
		row := docs[0].(map[string]any)
		if row["dataset_id"] != f.datasets["shared"] || row["chunk_count"] != float64(0) || row["token_count"] != float64(0) || row["run"] != "UNSTART" {
			t.Fatal("projection lost literal zero or scope", row)
		}
	}
	for _, actor := range []string{"", "invite", "revoked", "foreign", "disabled"} {
		_, result, _, _ := f.request(t, url, actor, "GET", "/datasets/"+f.datasets["shared"]+"/documents", nil)
		if result["code"] == float64(0) || result["data"] != nil {
			t.Fatal("denied document list", actor, result)
		}
	}
	for _, kind := range []string{"private", "foreign", "disabled"} {
		_, result, _, _ := f.request(t, url, "member", "GET", "/datasets/"+f.datasets[kind]+"/documents", nil)
		if result["code"] == float64(0) {
			t.Fatal("foreign/private/disabled read leaked", kind, result)
		}
	}
	_, result, _, _ := f.request(t, url, "owner", "GET", "/datasets/"+f.datasets["empty"]+"/documents", nil)
	data := result["data"].(map[string]any)
	if data["total"] != float64(0) || len(data["docs"].([]any)) != 0 {
		t.Fatal("empty collection lost", result)
	}
	// Frozen original pagination normalizes invalid values to defaults.
	for _, query := range []string{"?page=0", "?page=-1", "?page=nope", "?page_size=101"} {
		_, result, _, _ := f.request(t, url, "owner", "GET", "/datasets/"+f.datasets["shared"]+"/documents"+query, nil)
		if result["code"] != float64(0) || len(result["data"].(map[string]any)["docs"].([]any)) != 2 {
			t.Fatal("original pagination default changed", query, result)
		}
	}
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM document WHERE kb_id=?", f.datasets["shared"]).Scan(&count); err != nil || count != 2 {
		t.Fatal("independent list readback", count, err)
	}
	f.Journal.Event("canonical-document-list-readback", map[string]any{"actual_http_mysql": true, "pagination_total_selected_separate": true, "foreign_private_invite_revoked_disabled_denied": true, "literal_zero_empty_collection": true})
}
