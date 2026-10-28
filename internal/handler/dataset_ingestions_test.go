package handler

import (
	"net/url"
	"testing"
)

func TestIngestionQueryStrictSelection(t *testing.T) {
	for _, raw := range []string{"page=x", "page=%2B1", "page_size=%2B1", "page=-1", "page=1&page=2", "page_size=101", "page_size=NaN", "page_size=", "page_size=99999999999999999999999", "page=9223372036854775807&page_size=100", "desc=wat", "orderby=dsl", "orderby=create_time%3BDROP+TABLE+x", "log_type=missing", "operation_status=", "operation_status=9", "create_date_from=bad", "create_date_from=2026-01-02&create_date_to=2026-01-01", "types="} {
		q, _ := url.ParseQuery(raw)
		if _, err := ingestionQuery(q); err == nil {
			t.Fatal("accepted malformed query", raw)
		}
	}
	for _, raw := range []string{"", "keywords=", "page=0&page_size=0", "page=0&page_size=1", "page=1&page_size=100", "operation_status=0", "operation_status=DONE&operation_status=3", "create_date_from=2026-01-02T08%3A00%3A00%2B08%3A00&create_date_to=2026-01-02"} {
		q, _ := url.ParseQuery(raw)
		if _, err := ingestionQuery(q); err != nil {
			t.Fatal(raw, err)
		}
	}
}
