package handler

import (
	"multirag/internal/common"
	"multirag/internal/dao"
	"multirag/internal/service"
	"sync"
	"testing"
)

// Original team HTTP and actual SQL lifecycle; aggregate provider/Agent/metadata
// assertions live in their explicit scoped suites, with per-subcase dispositions.
func TestBatchGoAPI(t *testing.T) {
	f := newCanonicalHTTPFixture(t)
	svc := service.NewTenantService()
	h := NewTenantHandler(svc, service.NewUserService(), service.NewKnowledgebaseService())
	app := f.app()
	app.POST("/tenants/:tenant_id/users", h.AddTenantMember)
	app.DELETE("/tenants/:tenant_id/users", h.RemoveTenantMember)
	app.GET("/tenants/:tenant_id/users", h.ListTenantMembers)
	app.PATCH("/tenants/:tenant_id", h.AcceptTenantInvite)
	url := f.serve(t, app)
	owner, foreign := f.ids["owner"], f.ids["foreign"]
	_, denied, _, _ := f.request(t, url, "foreign", "POST", "/tenants/"+owner+"/users", map[string]any{"email": foreign + "@fixture.invalid"})
	if denied["code"] != float64(common.CodeAuthenticationError) {
		t.Fatal("nonowner invitation", denied)
	}
	_, created, _, _ := f.request(t, url, "owner", "POST", "/tenants/"+owner+"/users", map[string]any{"email": foreign + "@fixture.invalid"})
	if created["code"] != float64(0) {
		t.Fatal("invite", created)
	}
	access := dao.NewKnowledgebaseDAO()
	if access.Accessible(f.datasets["shared"], foreign) {
		t.Fatal("pending invite granted dataset")
	}
	_, accepted, _, _ := f.request(t, url, "foreign", "PATCH", "/tenants/"+owner, nil)
	if accepted["code"] != float64(0) || !access.Accessible(f.datasets["shared"], foreign) || access.Accessible(f.datasets["private"], foreign) {
		t.Fatal("invite accept scope", accepted)
	}
	_, denied, _, _ = f.request(t, url, "foreign", "DELETE", "/tenants/"+owner+"/users", map[string]any{"user_id": f.ids["member"]})
	if denied["code"] == float64(0) {
		t.Fatal("normal member removed someone else", denied)
	}
	_, left, _, _ := f.request(t, url, "foreign", "DELETE", "/tenants/"+owner+"/users", map[string]any{"user_id": foreign})
	if left["code"] != float64(0) || access.Accessible(f.datasets["shared"], foreign) {
		t.Fatal("leave did not revoke access", left)
	}
	_, denied, _, _ = f.request(t, url, "owner", "DELETE", "/tenants/"+owner+"/users", map[string]any{"user_id": owner})
	if denied["code"] != float64(common.CodeArgumentError) {
		t.Fatal("owner was removed", denied)
	}
	var wg sync.WaitGroup
	outcomes := make(chan bool, 8)
	for range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, code, err := svc.AddMember(owner, owner, &service.AddMemberRequest{Email: foreign + "@fixture.invalid"})
			outcomes <- err == nil && code == common.CodeSuccess
		}()
	}
	wg.Wait()
	close(outcomes)
	successes := 0
	for ok := range outcomes {
		if ok {
			successes++
		}
	}
	var count int
	if err := f.Read.QueryRow("SELECT count(*) FROM user_tenant WHERE user_id=? AND tenant_id=? AND status='1'", foreign, owner).Scan(&count); err != nil || count != 1 || successes != 1 {
		t.Fatal("concurrent duplicate invitations", successes, count, err)
	}
	_, listed, _, _ := f.request(t, url, "owner", "GET", "/tenants/"+owner+"/users", nil)
	if listed["code"] != float64(0) {
		t.Fatal("owner member listing", listed)
	}
	f.Journal.Event("canonical-team-http-sql-readback", map[string]any{"owner_invite_accept_remove": true, "pending_no_access_private_after_accept_denied": true, "concurrent_single_invitation": true, "independent_sql_readback": true})
}
