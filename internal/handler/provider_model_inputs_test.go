package handler_test

import (
	"encoding/json"
	"net/http"
	"testing"
)

// The path is authoritative for the original AddModel endpoint. This uses its
// actual authorization, binder, transaction and independent SQL readback.
func TestCustomModelRouteIdentity(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	f.enroll(t, "OpenAI", "declaration", "fixture-only-key", "http://127.0.0.1:1", "gpt-4o", "chat")
	path := "/api/v1/providers/OpenAI/instances/declaration/models"
	f.requireSuccess(t, "owner", http.MethodPost, path, map[string]any{"models": []any{map[string]any{"model_name": "custom-declared", "model_types": []string{"chat", "vision"}, "max_tokens": 0, "thinking": false}}})
	var count int
	if err := f.Read.QueryRow("SELECT COUNT(*) FROM tenant_model m JOIN tenant_model_instance i ON i.id=m.instance_id JOIN tenant_model_provider p ON p.id=m.provider_id WHERE p.tenant_id=? AND p.provider_name=? AND i.instance_name=? AND m.model_name=?", "owner-tenant", "OpenAI", "declaration", "custom-declared").Scan(&count); err != nil || count != 1 {
		t.Fatal("route identity was not persisted", count, err)
	}
	var rawExtra string
	if err := f.Read.QueryRow("SELECT extra FROM tenant_model WHERE model_name=?", "custom-declared").Scan(&rawExtra); err != nil {
		t.Fatal(err)
	}
	var extra map[string]any
	if json.Unmarshal([]byte(rawExtra), &extra) != nil || extra["max_tokens"] != float64(0) || extra["thinking"] != false {
		t.Fatal("explicit zero or false declaration lost", rawExtra)
	}
	// The frozen service stores the first capability as its primary type.
	if types, ok := extra["model_types"].([]any); !ok || len(types) != 1 || types[0] != "chat" {
		t.Fatal("frozen primary capability projection changed", rawExtra)
	}
	// The original typed int DTO treats omitted and null budgets as zero.
	// Verify that contract through the actual binder and persisted declaration.
	for _, declaration := range []map[string]any{
		{"model_name": "custom-omitted-budget", "model_types": []string{"chat"}},
		{"model_name": "custom-null-budget", "model_types": []string{"chat"}, "max_tokens": nil},
	} {
		f.requireSuccess(t, "owner", http.MethodPost, path, map[string]any{"models": []any{declaration}})
		if err := f.Read.QueryRow("SELECT extra FROM tenant_model WHERE model_name=?", declaration["model_name"]).Scan(&rawExtra); err != nil {
			t.Fatal(err)
		}
		if json.Unmarshal([]byte(rawExtra), &extra) != nil || extra["max_tokens"] != float64(0) {
			t.Fatal("original optional budget did not persist zero", rawExtra)
		}
	}
	before := f.snapshot(t)
	f.Journal.Event("reserve-sql-trigger", map[string]any{"database": f.Database, "trigger": "provider_reject_model_insert"})
	if err := f.DB.Exec("CREATE TRIGGER provider_reject_model_insert BEFORE INSERT ON tenant_model FOR EACH ROW BEGIN IF NEW.model_name='rollback-blocked' THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='controlled insert failure'; END IF; END").Error; err != nil {
		t.Fatal(err)
	}
	_, rollback, _ := f.request(t, "owner", http.MethodPost, path, map[string]any{"models": []any{map[string]any{"model_name": "rollback-first", "model_types": []string{"chat"}}, map[string]any{"model_name": "rollback-blocked", "model_types": []string{"chat"}}}})
	if providerEnvelopeError(rollback) == nil || f.snapshot(t) != before {
		t.Fatal("physical SQL model batch failure partially committed")
	}
	if err := f.DB.Exec("DROP TRIGGER provider_reject_model_insert").Error; err != nil {
		t.Fatal(err)
	}
	for index, body := range []any{
		nil,
		map[string]any{},
		map[string]any{"provider_name": "Other", "models": []any{map[string]any{"model_name": "wrong-provider", "model_types": []string{"chat"}}}},
		map[string]any{"instance_name": "other", "models": []any{map[string]any{"model_name": "wrong-instance", "model_types": []string{"chat"}}}},
		map[string]any{"models": []any{}},
		map[string]any{"models": []any{map[string]any{"model_name": "missing-type"}}},
		map[string]any{"models": []any{map[string]any{"model_name": " ", "model_types": []string{"chat"}}}},
		map[string]any{"models": []any{map[string]any{"model_name": "bad-type", "model_types": []string{"invalid-capability"}}}},
		map[string]any{"models": []any{map[string]any{"model_name": "negative-budget", "model_types": []string{"chat"}, "max_tokens": -1}}},
		map[string]any{"models": []any{map[string]any{"model_name": "fraction-budget", "model_types": []string{"chat"}, "max_tokens": 0.5}}},
		map[string]any{"models": []any{map[string]any{"model_name": "string-budget", "model_types": []string{"chat"}, "max_tokens": "2"}}},
		map[string]any{"models": []any{map[string]any{"model_name": "boolean-budget", "model_types": []string{"chat"}, "max_tokens": true}}},
		map[string]any{"models": []any{map[string]any{"model_name": "overflow-budget", "model_types": []string{"chat"}, "max_tokens": 1e100}}},
		map[string]any{"models": []any{map[string]any{"model_name": "string-thinking", "model_types": []string{"chat"}, "thinking": "false"}}},
		map[string]any{"models": []any{map[string]any{"model_name": "atomic-first", "model_types": []string{"chat"}}, map[string]any{"model_name": "atomic-second", "model_types": []string{"invalid-capability"}}}},
	} {
		status, result, _ := f.request(t, "owner", http.MethodPost, path, body)
		if status == http.StatusOK && result["code"] == float64(0) {
			t.Fatalf("invalid declaration case %d accepted", index)
		}
		if f.snapshot(t) != before {
			t.Fatal("rejected declaration changed SQL")
		}
	}
	for _, who := range []string{"foreign", "inactive", "invited", "revoked"} {
		status, result, _ := f.request(t, who, http.MethodPost, path, map[string]any{"models": []any{map[string]any{"model_name": "unauthorized", "model_types": []string{"chat"}}}})
		if status == http.StatusOK && result["code"] == float64(0) {
			t.Fatal("unauthorized declaration accepted", who)
		}
		if f.snapshot(t) != before {
			t.Fatal("unauthorized declaration changed SQL", who)
		}
	}
}
