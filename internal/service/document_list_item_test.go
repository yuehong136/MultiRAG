package service

import (
	"multirag/internal/entity"
	"testing"
	"time"
)

func TestDocumentListItemStatusTimesAndJSON(t *testing.T) {
	for _, tc := range []struct {
		value *string
		want  string
	}{{nil, "UNSTART"}, {docListString("0"), "UNSTART"}, {docListString("1"), "RUNNING"}, {docListString("2"), "CANCEL"}, {docListString("3"), "DONE"}, {docListString("4"), "FAIL"}, {docListString(" DONE "), "DONE"}} {
		if got := mapDocumentRunStatus(tc.value); got != tc.want {
			t.Fatal(got, tc.want)
		}
	}
	zero := int64(0)
	date := time.Date(2026, 10, 24, 3, 0, 0, 0, time.UTC)
	row := mapDocumentListItem(&entity.DocumentListItem{Document: entity.Document{ID: "d", ParserID: "naive", ParserConfig: entity.JSONMap{"enabled": false}, TokenNum: 4, ChunkNum: 2, BaseModel: entity.BaseModel{CreateTime: &zero, CreateDate: &date}}})
	if row["create_time"] != int64(0) || row["update_time"] != int64(0) || row["create_date"] != "2026-10-24 03:00:00" || row["update_date"] != "" || row["run"] != "UNSTART" || row["parser_config"].(entity.JSONMap)["enabled"] != false || row["token_count"] != int64(4) || row["chunk_count"] != int64(2) {
		t.Fatal(row)
	}
	if _, old := row["created_at"]; old {
		t.Fatal("retired timestamp alias")
	}
}
func docListString(value string) *string { return &value }
