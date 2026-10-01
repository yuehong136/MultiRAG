package dao

import (
	"multirag/internal/server"
	"net/url"
	"strings"
	"testing"
)

func TestPostgresDSNPoolSchemaAndEscaping(t *testing.T) {
	cfg := server.DatabaseConfig{Host: "127.0.0.1", Port: 5432, Username: "task-user", Password: "a @'$\\", Database: "scratch"}
	dsn, err := PostgresDSN(cfg)
	if err != nil {
		t.Fatal(err)
	}
	u, err := url.Parse(dsn)
	if err != nil {
		t.Fatal(err)
	}
	password, _ := u.User.Password()
	if password != cfg.Password || u.Query().Get("search_path") != "usr_ai,public" {
		t.Fatal("connection parameters did not preserve credentials or schema")
	}
	for _, schema := range []string{"usr_ai; DROP SCHEMA public", "x,public", strings.Repeat("a", 64)} {
		cfg.Schema = schema
		if _, err := PostgresDSN(cfg); err == nil {
			t.Fatal("unsafe schema accepted")
		}
	}
}
