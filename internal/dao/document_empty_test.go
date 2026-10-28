package dao

import (
	"context"
	"testing"
)

func TestDocumentGetByIDsEmptyDoesNotAccessDB(t *testing.T) {
	originalDB := DB
	DB = nil
	t.Cleanup(func() { DB = originalDB })

	for _, tc := range []struct {
		name string
		ids  []string
	}{
		{name: "nil", ids: nil},
		{name: "empty", ids: []string{}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			docs, err := NewDocumentDAO().GetByIDs(context.Background(), tc.ids)
			if docs != nil || err != nil {
				t.Fatalf("GetByIDs(%v) = %#v, %v; want nil, nil", tc.ids, docs, err)
			}
		})
	}
}
