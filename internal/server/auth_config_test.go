package server

import (
	"errors"
	"testing"
	"time"
)

type lostSecretStore struct{}

func (lostSecretStore) Get(string) (string, error)               { return "", nil }
func (lostSecretStore) Set(string, string, time.Duration) bool   { return false }
func (lostSecretStore) SetNX(string, string, time.Duration) bool { return false }

type failedSecretStore struct{ lostSecretStore }

func (failedSecretStore) Get(string) (string, error) { return "", errors.New("unavailable") }
func TestSecretKeyRequiresPersistedCandidate(t *testing.T) {
	previous := globalConfig
	t.Cleanup(func() { globalConfig = previous })
	key := "owned-test-key-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
	globalConfig = &Config{Server: ServerConfig{SecretKey: &key}}
	if got, err := GetSecretKey(nil); err != nil || got != key {
		t.Fatal(got, err)
	}
	globalConfig.Server.SecretKey = nil
	for _, store := range []VariableStore{nil, lostSecretStore{}, failedSecretStore{}} {
		if got, err := GetSecretKey(store); err == nil || got != "" {
			t.Fatal("unpersisted signing key", got, err)
		}
	}
	for _, candidate := range []string{"", "candidate"} {
		if _, err := GetOrCreateKey(lostSecretStore{}, "owned", candidate); err == nil {
			t.Fatal("unpersisted value accepted")
		}
	}
}
