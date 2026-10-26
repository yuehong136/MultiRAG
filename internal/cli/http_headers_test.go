package cli

import "testing"

func TestWebTokenHeaders(t *testing.T) {
	client := &HTTPClient{APIKey: "scoped-token"}
	if client.Headers("web", nil)["Authorization"] != "Bearer scoped-token" {
		t.Fatal("web CLI dropped explicit token")
	}
	if client.Headers("admin", nil)["Authorization"] != "" {
		t.Fatal("user token selected for admin")
	}
	client.LoginToken = "login-token"
	if client.Headers("web", nil)["Authorization"] != "login-token" {
		t.Fatal("login credential precedence changed")
	}
	if client.Headers("api", nil)["Authorization"] != "Bearer scoped-token" {
		t.Fatal("API credential precedence changed")
	}
}
