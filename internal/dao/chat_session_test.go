package dao

import (
	"math"
	"testing"
)

func TestAgentSessionQueryBounds(t *testing.T) {
	for _, pair := range [][2]int{{0, 30}, {-1, 30}, {1, -1}, {1, 1001}, {math.MaxInt, 30}} {
		if err := ValidateAgentSessionPagination(pair[0], pair[1]); err == nil {
			t.Fatal(pair)
		}
	}
	for _, pair := range [][2]int{{1, 0}, {1, 30}, {2, 1000}} {
		if err := ValidateAgentSessionPagination(pair[0], pair[1]); err != nil {
			t.Fatal(pair, err)
		}
	}
	for _, key := range []string{"id", "name", "create_date", "update_time", "round"} {
		if !ValidAgentSessionOrderBy(key) {
			t.Fatal(key)
		}
	}
	for _, key := range []string{"", "update_time desc; DROP TABLE x", "user_id"} {
		if ValidAgentSessionOrderBy(key) {
			t.Fatal(key)
		}
	}
}
