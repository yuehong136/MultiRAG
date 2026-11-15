package utility

import "testing"

func TestAccessTokenCanonicalSignature(t *testing.T) {
	key := "owned-test-key-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
	token, err := DumpAccessToken("owned-access-token", key)
	if err != nil {
		t.Fatal(err)
	}
	for _, value := range []string{token, "Bearer " + token} {
		if got, err := ExtractAccessToken(value, key); err != nil || got != "owned-access-token" {
			t.Fatal(got, err)
		}
	}
	for _, value := range []string{"", token + "tampered", "Bearer invalid"} {
		if _, err := ExtractAccessToken(value, key); err == nil {
			t.Fatal("invalid signature accepted")
		}
	}
	if _, err := ExtractAccessToken(token, key+"wrong"); err == nil {
		t.Fatal("wrong signing key accepted")
	}
	if _, err := DumpAccessToken("", key); err == nil {
		t.Fatal("empty access identity accepted")
	}
}
