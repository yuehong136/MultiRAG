package handler_test

import (
	"encoding/json"
	"multirag/internal/dao"
	"multirag/internal/entity"
	"net/http"
	"strings"
	"testing"
)

func TestHybridAuthLiveServer(t *testing.T) {
	f := newCanonicalProviderFixture(t)
	passwordHash := "fixture-stored-password-hash-never-returned"
	if err := dao.DB.Model(&entity.User{}).Where("id=?", "owner").Update("password", passwordHash).Error; err != nil {
		t.Fatal(err)
	}
	before := f.snapshot(t)
	result := f.requireSuccess(t, "owner", "GET", "/api/v1/users/me", nil)
	profile := result["data"].(map[string]any)
	_, hasPassword := profile["password"]
	encoded, err := json.Marshal(profile)
	if err != nil || profile["id"] != "owner" || hasPassword || strings.Contains(string(encoded), passwordHash) {
		t.Fatal("profile leaked credentials or changed identity")
	}
	var user entity.User
	if err := dao.DB.Where("id=?", "owner").First(&user).Error; err != nil {
		t.Fatal(err)
	}
	if profile["access_token"] != *user.AccessToken {
		t.Fatal("frozen own-profile access token field changed")
	}
	foreign := f.requireSuccess(t, "foreign", "GET", "/api/v1/users/me", nil)["data"].(map[string]any)
	if foreign["id"] != "foreign" || foreign["access_token"] == *user.AccessToken {
		t.Fatal("profile crossed identity boundary")
	}
	for _, value := range []string{"malformed", *user.AccessToken, f.tokens["owner"] + "tampered", f.tokens["inactive"]} {
		req, _ := http.NewRequest("GET", f.url+"/api/v1/users/me", nil)
		req.Header.Set("Authorization", "Bearer "+value)
		res, err := f.client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		res.Body.Close()
		if res.StatusCode != 401 {
			t.Fatal("unsigned/tampered/disabled token accepted")
		}
	}
	if f.snapshot(t) != before {
		t.Fatal("identity read or refusal mutated SQL")
	}
	if strings.Contains(f.tokens["owner"], *user.AccessToken) {
		t.Fatal("signed token exposed raw credential")
	}
}
