package service

import (
	"github.com/google/uuid"
	"multirag/internal/utility"
	"strings"
	"testing"
)

func TestDatasetIDAcceptsNewAndHistoricalEntities(t *testing.T) {
	historical, err := uuid.NewUUID()
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{historical.String(), utility.GenerateToken()} {
		normalized, err := normalizeDatasetID(id)
		if err != nil || len(normalized) != 32 || strings.Contains(normalized, "-") {
			t.Fatal(id, normalized, err)
		}
	}
	for _, id := range []string{"", strings.Repeat("0", 32), "not-a-dataset", "9007199254740993"} {
		if _, err := normalizeDatasetID(id); err == nil {
			t.Fatal("invalid entity ID accepted", id)
		}
	}
}
