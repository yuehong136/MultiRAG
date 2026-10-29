package handler

import "testing"

func TestAgentSessionQueryContract(t *testing.T) {
	for _, raw := range []string{"page=bad", "page=%zz", "page=1;page_size=2", "page=1&page=2", "dsl=", "desc=other", "from_date=", "orderby=", "exp_user_id=a&exp_user_id=b"} {
		if _, err := parseAgentSessionQuery(raw); err == nil {
			t.Fatalf("accepted invalid query %q", raw)
		}
	}
	req, err := parseAgentSessionQuery("user_id=filtered&exp_user_id=explore&page=2&page_size=0&desc=False&dsl=false&keywords=hello&orderby=id")
	if err != nil || req.UserID != "filtered" || req.ExpUserID != "explore" || req.Page != 2 || req.PageSize != 0 || req.Desc || req.IncludeDSL || req.OrderBy != "id" {
		t.Fatal(req, err)
	}
	req, err = parseAgentSessionQuery("")
	if err != nil || req.Page != 1 || req.PageSize != 30 || req.OrderBy != "update_time" || !req.Desc || !req.IncludeDSL {
		t.Fatal(req, err)
	}
}
