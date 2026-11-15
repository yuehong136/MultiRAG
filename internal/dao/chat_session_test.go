package dao

import (
	"encoding/json"
	"math"
	"multirag/internal/entity"
	"multirag/internal/testsupport/scratch"
	"testing"
)

func TestAgentSessionQueryBounds(t *testing.T) {
	f := scratch.New(t)
	old := DB
	DB = f.DB
	t.Cleanup(func() { DB = old })
	if err := DB.AutoMigrate(&entity.API4Conversation{}); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"id", "name", "create_date", "update_time", "round"} {
		if normalizeAgentSessionOrderBy(key) != key {
			t.Fatal(key)
		}
	}
	for _, key := range []string{"", "update_time desc; DROP TABLE x", "user_id"} {
		if normalizeAgentSessionOrderBy(key) != "update_time" {
			t.Fatal(key)
		}
	}
	if err := DB.Create(&entity.API4Conversation{ID: "session", DialogID: "agent", UserID: "owner", Message: json.RawMessage(`[]`), DSL: entity.JSONMap{"secret": true}}).Error; err != nil {
		t.Fatal(err)
	}
	for _, pair := range [][2]int{{0, 0}, {-1, -1}, {1, 30}} {
		total, rows, err := NewChatSessionDAO().ListAgentSessions(ListAgentSessionsParams{AgentID: "agent", Page: pair[0], PageSize: pair[1], OrderBy: "update_time desc; DROP TABLE x"})
		if err != nil || total != 1 || len(rows) != 1 || rows[0].DSL != nil {
			t.Fatalf("target defaults/safe order/DSL omission failed: %d %+v %v", total, rows, err)
		}
	}
	_, _, err := NewChatSessionDAO().ListAgentSessions(ListAgentSessionsParams{AgentID: "agent", Page: math.MaxInt, PageSize: 30})
	if err == nil {
		t.Fatal("overflow pagination accepted")
	}
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM api_4_conversation").Scan(&count); err != nil || count != 1 {
		t.Fatal("invalid query changed sessions", err)
	}
}
