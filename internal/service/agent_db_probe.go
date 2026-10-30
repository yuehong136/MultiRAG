package service

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net"
	"strconv"
	"strings"
	"time"
	"unicode"

	"github.com/go-sql-driver/mysql"
	"multirag/internal/common"
	"multirag/internal/utility"
)

type TestDBConnectionRequest struct {
	DBType   string          `json:"db_type"`
	Database string          `json:"database"`
	Username string          `json:"username"`
	Host     string          `json:"host"`
	Port     json.RawMessage `json:"port"`
	Password string          `json:"password"`
}

const databaseProbeTimeout = 5 * time.Second

func databaseProbePort(raw json.RawMessage) (string, error) {
	value := string(raw)
	if strings.HasPrefix(value, `"`) {
		if json.Unmarshal(raw, &value) != nil {
			return "", errors.New("Invalid database port.")
		}
	}
	if value == "" || strings.IndexFunc(value, func(r rune) bool { return r < '0' || r > '9' }) >= 0 {
		return "", errors.New("Invalid database port.")
	}
	port, err := strconv.Atoi(value)
	if err != nil || port < 1 || port > 65535 {
		return "", errors.New("Invalid database port.")
	}
	return strconv.Itoa(port), nil
}

func openDatabaseProbe(config *mysql.Config) (*sql.DB, error) {
	connector, err := mysql.NewConnector(config)
	if err != nil {
		return nil, err
	}
	return sql.OpenDB(connector), nil
}

// TestDBConnection is a bounded read-only MySQL-family probe. No caller-controlled
// SQL, DSN parameters, file loading, multiple statements or unpinned DNS are used.
func (s *AgentService) TestDBConnection(ctx context.Context, actorID string, req TestDBConnectionRequest) (common.ErrorCode, error) {
	if actorID == "" {
		return common.CodeAuthenticationError, errors.New("No authorization.")
	}
	return probeDatabase(ctx, req, utility.AssertURLSafe, openDatabaseProbe)
}

func probeDatabase(ctx context.Context, req TestDBConnectionRequest, resolve func(context.Context, string) (string, string, error), open func(*mysql.Config) (*sql.DB, error)) (code common.ErrorCode, err error) {
	if req.DBType != "mysql" && req.DBType != "mariadb" && req.DBType != "oceanbase" {
		return common.CodeArgumentError, errors.New("Unsupported database type.")
	}
	if strings.TrimSpace(req.Database) == "" || strings.TrimSpace(req.Username) == "" || req.Password == "" || req.Host == "" {
		return common.CodeArgumentError, errors.New("Database, username, password, host and port are required.")
	}
	port, err := databaseProbePort(req.Port)
	if err != nil {
		return common.CodeArgumentError, err
	}
	// A bare ASCII host is the only accepted destination syntax. Constructing a
	// URL from it reuses the shared all-answers public-IP policy without accepting
	// an arbitrary user URL, path, userinfo, zone, port or query.
	host := req.Host
	if strings.ContainsAny(host, "/\\@?#%[]") || strings.ContainsFunc(host, func(r rune) bool { return r > 127 || unicode.IsSpace(r) || unicode.IsControl(r) }) || (strings.Contains(host, ":") && net.ParseIP(host) == nil) {
		return common.CodeArgumentError, errors.New("Invalid database host.")
	}
	ctx, cancel := context.WithTimeout(ctx, databaseProbeTimeout)
	defer cancel()
	_, pin, err := resolve(ctx, "http://"+net.JoinHostPort(host, port))
	if err != nil || ctx.Err() != nil || net.ParseIP(pin) == nil {
		return common.CodeDataError, errors.New("Database host must resolve only to public addresses.")
	}
	config := mysql.NewConfig()
	config.User, config.Passwd, config.DBName = req.Username, req.Password, req.Database
	config.Net, config.Addr = "tcp", net.JoinHostPort(pin, port)
	config.Timeout, config.ReadTimeout, config.WriteTimeout = databaseProbeTimeout, databaseProbeTimeout, databaseProbeTimeout
	if req.DBType == "oceanbase" {
		config.Params = map[string]string{"charset": "utf8mb4"}
	}
	db, err := open(config)
	if err != nil {
		return common.CodeExceptionError, errors.New("Database connection failed.")
	}
	defer func() {
		if closeErr := db.Close(); closeErr != nil && err == nil {
			code, err = common.CodeExceptionError, errors.New("Database connection failed.")
		}
	}()
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(0)
	var one int
	if err := db.QueryRowContext(ctx, "SELECT 1").Scan(&one); err != nil || one != 1 {
		return common.CodeExceptionError, errors.New("Database connection failed.")
	}
	return common.CodeSuccess, nil
}
