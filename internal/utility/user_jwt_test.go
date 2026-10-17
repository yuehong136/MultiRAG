package utility

import (
	"github.com/golang-jwt/jwt/v4"
	"testing"
	"time"
)

func TestMainAPIJWTRequiresHS256SubjectAndExpiry(t *testing.T) {
	key := "owned-test-key-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
	token, err := SignUserJWT("user@example.invalid", key)
	if err != nil {
		t.Fatal(err)
	}
	if subject, err := UserJWTSubject("Bearer "+token, key); err != nil || subject != "user@example.invalid" {
		t.Fatal("JWT roundtrip", err)
	}
	for _, claims := range []jwt.RegisteredClaims{
		{Subject: "user@example.invalid"},
		{Subject: "user@example.invalid", ExpiresAt: jwt.NewNumericDate(time.Now().Add(-time.Minute))},
		{ExpiresAt: jwt.NewNumericDate(time.Now().Add(time.Minute))},
	} {
		for _, algorithm := range []*jwt.SigningMethodHMAC{jwt.SigningMethodHS256, jwt.SigningMethodHS384} {
			invalid, _ := jwt.NewWithClaims(algorithm, claims).SignedString([]byte(key))
			if _, err := UserJWTSubject(invalid, key); err == nil {
				t.Fatal("invalid JWT accepted")
			}
		}
	}
	if _, err := UserJWTSubject(token, key+"wrong"); err == nil {
		t.Fatal("wrong key accepted")
	}
	if _, err := SignUserJWT("user@example.invalid", ""); err == nil {
		t.Fatal("missing key accepted")
	}
}
