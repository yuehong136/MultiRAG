package admin

import (
	"strings"
	"testing"
)

func TestAdminUnsupportedOperationsHTTP(t *testing.T) {
	f := newAdminFixture(t)
	before := adminDeletionSnapshot(t, f, "admin")
	cases := []struct{ method, path, body string }{{"DELETE", "/ingestors", `{"ingestor_name":"worker"}`}, {"GET", "/roles", ""}, {"POST", "/roles", `{"role_name":"role"}`}, {"GET", "/roles/role", ""}, {"PUT", "/roles/role", `{"description":"description"}`}, {"DELETE", "/roles/role", ""}, {"GET", "/roles/role/permission", ""}, {"POST", "/roles/role/permission", `{"actions":["read"],"resource":"data"}`}, {"DELETE", "/roles/role/permission", `{"actions":["read"],"resource":"data"}`}, {"PUT", "/users/admin@fixture.invalid/role", `{"role_name":"role"}`}, {"GET", "/users/admin@fixture.invalid/permission", ""}, {"GET", "/users/admin@fixture.invalid/datasets", ""}, {"GET", "/users/admin@fixture.invalid/agents", ""}, {"DELETE", "/services/worker", ""}, {"PUT", "/services/worker", ""}, {"GET", "/sandbox/providers", ""}, {"GET", "/sandbox/providers/provider/schema", ""}, {"GET", "/sandbox/config", ""}, {"POST", "/sandbox/config", `{"provider_type":"provider","config":{"token":"fixture"},"set_active":false}`}, {"POST", "/sandbox/test", `{"provider_type":"provider","config":{}}`}}
	for _, tc := range cases {
		t.Run(tc.method+tc.path, func(t *testing.T) {
			status, result := f.request(t, "admin", tc.method, tc.path, tc.body)
			if status < 400 || result["code"] == float64(0) || !strings.Contains(result["message"].(string), "not implemented") {
				t.Fatal("unsupported action acknowledged", status, result)
			}
			if adminDeletionSnapshot(t, f, "admin") != before {
				t.Fatal("unsupported operation changed persisted state")
			}
		})
	}
}
