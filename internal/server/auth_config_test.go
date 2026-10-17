package server

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

type lostSecretStore struct{}

func (lostSecretStore) Get(string) (string, error)               { return "", nil }
func (lostSecretStore) Set(string, string, time.Duration) bool   { return false }
func (lostSecretStore) SetNX(string, string, time.Duration) bool { return false }

type failedSecretStore struct{ lostSecretStore }

func (failedSecretStore) Get(string) (string, error) { return "", errors.New("unavailable") }

func TestAuthConfigurationAndSecretFailure(t *testing.T) {
	previousConfig, previousVariables := globalConfig, globalVariables
	t.Cleanup(func() { globalConfig = previousConfig; globalVariables = previousVariables })
	config := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(config, []byte("multirag:\n  secret_key: 'owned-test-key-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'\nauthentication:\n  enable_register: false\n  disable_password_login: true\n"), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("DISABLE_PASSWORD_LOGIN", "false")
	if err := FromConfigFile(config); err != nil {
		t.Fatal(err)
	}
	if globalConfig.RegisterEnabled != 0 || globalConfig.Authentication.DisablePasswordLogin {
		t.Fatal("auth switch precedence")
	}
	globalVariables = nil
	if err := InitVariables(nil); err != nil {
		t.Fatal("fixed key must not use Redis", err)
	}
	if GetSecretKey() != globalConfig.Server.SecretKey {
		t.Fatal("configured key not used")
	}
	globalVariables = nil
	globalConfig.Server.SecretKey = ""
	if err := InitVariables(failedSecretStore{}); err == nil {
		t.Fatal("Redis failure accepted")
	}
	if GetSecretKey() != "" {
		t.Fatal("uninitialized fallback key")
	}
	if _, err := GetOrCreateKey(lostSecretStore{}, "owned", "candidate"); err == nil {
		t.Fatal("unpersisted losing candidate accepted")
	}
	if _, err := GetOrCreateKey(lostSecretStore{}, "owned", ""); err == nil {
		t.Fatal("empty candidate accepted")
	}
	t.Setenv("REGISTER_ENABLED", "invalid")
	if err := FromConfigFile(config); err == nil {
		t.Fatal("malformed authentication flag accepted")
	}
}
