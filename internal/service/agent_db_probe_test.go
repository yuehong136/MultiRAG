package service

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"testing"
	"time"

	"github.com/go-sql-driver/mysql"
	"multirag/internal/common"
	"multirag/internal/utility"
)

func probeRequest() TestDBConnectionRequest {
	return TestDBConnectionRequest{DBType: "mysql", Database: "fixture", Username: "user", Password: "private-driver-marker", Host: "database.invalid", Port: json.RawMessage(`3306`)}
}

type probeDriver struct {
	queries, closed, rowsClosed int
	queryErr, closeErr          error
	one                         int64
	block                       bool
}

type probeConnector struct{ connection *probeDriver }

func (p *probeConnector) Connect(ctx context.Context) (driver.Conn, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return p.connection, nil
}
func (p *probeConnector) Driver() driver.Driver { return &probeDriverFactory{p.connection} }

type probeDriverFactory struct{ connection *probeDriver }

func (p *probeDriverFactory) Open(string) (driver.Conn, error) { return p.connection, nil }
func (p *probeDriver) Prepare(string) (driver.Stmt, error) {
	return nil, errors.New("unexpected prepare")
}
func (p *probeDriver) Begin() (driver.Tx, error) { return nil, errors.New("unexpected transaction") }
func (p *probeDriver) Close() error              { p.closed++; return p.closeErr }
func (p *probeDriver) QueryContext(ctx context.Context, query string, args []driver.NamedValue) (driver.Rows, error) {
	p.queries++
	if query != "SELECT 1" || len(args) != 0 {
		return nil, errors.New("unexpected statement")
	}
	if p.block {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	if p.queryErr != nil {
		return nil, p.queryErr
	}
	return &probeRows{parent: p}, nil
}

type probeRows struct {
	parent *probeDriver
	read   bool
}

func (r *probeRows) Columns() []string { return []string{"1"} }
func (r *probeRows) Close() error      { r.parent.rowsClosed++; return nil }
func (r *probeRows) Next(values []driver.Value) error {
	if r.read {
		return io.EOF
	}
	r.read = true
	values[0] = r.parent.one
	return nil
}

func TestAgentDBProbeValidationBeforeResolveOrOpen(t *testing.T) {
	for _, raw := range []string{`null`, `true`, `3306.5`, `3306.0`, `3e3`, `-1`, `0`, `65536`, `" 3306"`, `"3306?x=1"`, `{}`, `[]`} {
		if _, err := databaseProbePort(json.RawMessage(raw)); err == nil {
			t.Fatalf("accepted port %s", raw)
		}
	}
	for _, raw := range []string{`3306`, `"3306"`, `1`, `65535`} {
		if _, err := databaseProbePort(json.RawMessage(raw)); err != nil {
			t.Fatal(raw, err)
		}
	}
	for _, host := range []string{"http://database.invalid", "database.invalid:3306", "user@database.invalid", "database.invalid/path", "database.invalid?x=y", "database.invalid#fragment", "[::1]", "fe80::1%en0", " database.invalid", "例子.example", "database.invalid\\path"} {
		req := probeRequest()
		req.Host = host
		code, err := probeDatabase(t.Context(), req, func(context.Context, string) (string, string, error) {
			t.Fatal("validation performed DNS")
			return "", "", nil
		}, func(*mysql.Config) (*sql.DB, error) { t.Fatal("validation opened driver"); return nil, nil })
		if err == nil || code != common.CodeArgumentError {
			t.Fatal(host, code, err)
		}
	}
	for _, typ := range []string{"postgres", "sqlite", "MYSQL", "mysql?multiStatements=true"} {
		req := probeRequest()
		req.DBType = typ
		code, err := probeDatabase(t.Context(), req, nil, nil)
		if err == nil || code != common.CodeArgumentError {
			t.Fatal(typ, code, err)
		}
	}
}

func TestAgentDBProbePolicyAndCancellationBeforeOpen(t *testing.T) {
	for _, host := range []string{"127.0.0.1", "::1", "::ffff:127.0.0.1", "10.0.0.1", "192.0.2.1", "64:ff9b::7f00:1", "2001:db8::1"} {
		req := probeRequest()
		req.Host = host
		code, err := probeDatabase(t.Context(), req, utility.AssertURLSafe, func(*mysql.Config) (*sql.DB, error) { t.Fatal("unsafe host opened driver"); return nil, nil })
		if err == nil || code != common.CodeDataError {
			t.Fatal(host, code, err)
		}
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	code, err := probeDatabase(ctx, probeRequest(), func(ctx context.Context, _ string) (string, string, error) { return "", "", ctx.Err() }, func(*mysql.Config) (*sql.DB, error) { t.Fatal("cancelled resolve opened driver"); return nil, nil })
	if err == nil || code != common.CodeDataError {
		t.Fatal(code, err)
	}
}

func TestAgentDBProbePinReadOnlyAndLifecycle(t *testing.T) {
	for _, scenario := range []string{"success", "query-failure", "bad-result", "cancel"} {
		t.Run(scenario, func(t *testing.T) {
			p := &probeDriver{one: 1}
			switch scenario {
			case "query-failure":
				p.queryErr = errors.New("private-driver-marker")
			case "bad-result":
				p.one = 2
			case "cancel":
				p.block = true
			}
			ctx, cancel := context.WithTimeout(t.Context(), 40*time.Millisecond)
			defer cancel()
			req := probeRequest()
			req.DBType = "oceanbase"
			code, err := probeDatabase(ctx, req, func(ctx context.Context, endpoint string) (string, string, error) {
				if endpoint != "http://database.invalid:3306" {
					t.Fatal(endpoint)
				}
				deadline, ok := ctx.Deadline()
				if !ok || time.Until(deadline) > databaseProbeTimeout {
					t.Fatal("unbounded DNS")
				}
				return "database.invalid", "8.8.8.8", nil
			}, func(cfg *mysql.Config) (*sql.DB, error) {
				if cfg.Addr != "8.8.8.8:3306" || cfg.Net != "tcp" || cfg.User != req.Username || cfg.Passwd != req.Password || cfg.DBName != req.Database || cfg.MultiStatements || cfg.AllowAllFiles || cfg.Params["charset"] != "utf8mb4" || cfg.Timeout != databaseProbeTimeout || cfg.ReadTimeout != databaseProbeTimeout || cfg.WriteTimeout != databaseProbeTimeout {
					t.Fatal("unsafe driver configuration")
				}
				return sql.OpenDB(&probeConnector{p}), nil
			})
			if scenario == "success" {
				if err != nil || code != common.CodeSuccess || p.rowsClosed != 1 {
					t.Fatal(code, err, p)
				}
			} else if err == nil || code != common.CodeExceptionError || strings.Contains(err.Error(), "private-driver-marker") {
				t.Fatal(code, err)
			}
			if p.closed != 1 || p.queries != 1 {
				t.Fatal("connection/query lifecycle", p)
			}
		})
	}
	code, err := probeDatabase(t.Context(), probeRequest(), func(context.Context, string) (string, string, error) { return "database.invalid", "8.8.8.8", nil }, func(*mysql.Config) (*sql.DB, error) { return nil, errors.New("private-driver-marker") })
	if err == nil || code != common.CodeExceptionError || strings.Contains(err.Error(), "private-driver-marker") {
		t.Fatal(code, err)
	}
}
