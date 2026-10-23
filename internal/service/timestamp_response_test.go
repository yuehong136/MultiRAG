package service

import (
	"multirag/internal/entity"
	"testing"
	"time"
)

func TestDocumentTimestampResponse(t *testing.T) {
	for _, stamp := range []int64{0, 1700000000, 1700000000123, 1_000_000_000_000} {
		doc := &entity.Document{BaseModel: entity.BaseModel{CreateTime: &stamp, UpdateTime: &stamp}}
		got := (&DocumentService{}).toResponse(doc)
		secs := stamp
		if stamp >= 1_000_000_000_000 {
			secs /= 1000
		}
		expected := time.Unix(secs, 0).Format("2006-01-02 15:04:05")
		if got.CreatedAt != expected || got.UpdatedAt != expected {
			t.Fatalf("stamp %d: creation/update display mismatch", stamp)
		}
	}
	got := (&DocumentService{}).toResponse(&entity.Document{})
	if got.CreatedAt != "" || got.UpdatedAt != "" {
		t.Fatal("absent timestamps must stay absent")
	}
}
