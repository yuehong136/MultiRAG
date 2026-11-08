//
//  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
//
//  Licensed under the Apache License, Version 2.0 (the "License");
//  you may not use this file except in compliance with the License.
//  You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
//

// Package tool — ExeSQL tool (Phase 3 batch 1).
//
// ExeSQL lets an Agent component execute a SQL statement on a
// user-configured external database and return the rows as
// column→value maps. It mirrors the Python `agent/tools/exesql.py`
// semantics: per-invocation open/close of a fresh `database/sql` connection
// scoped to the tool's params (host/port/database/username/password),
// no ORM, no GORM — those layers are for RAGFlow's own metadata DB
// (`internal/dao`) and would be the wrong abstraction here.
//
// Why `database/sql` (not GORM, not sqlx):
//   - The Python equivalent uses `pymysql.connect()` / `psycopg2.connect()`
//     directly with no ORM in the path.
//   - ExeSQL returns `[]map[string]any` (dynamic schema, LLM-supplied
//     SQL), so there is nothing to struct-scan — sqlx's headline
//     feature would be unused.
//   - GORM is for object-relational mapping of RAGFlow's metadata
//     tables. Reusing `internal/dao`'s GORM here would couple the
//     tool to RAGFlow's own DB pool and require an `internal/dao`
//     connection for a tool that has no business touching RAGFlow's
//     metadata at all.
//   - `database/sql` is stdlib, no extra runtime dependency beyond
//     the per-driver package (`go-sql-driver/mysql`, `lib/pq`,
//     `denisenkom/go-mssqldb`).
package tool

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	// SQL drivers — registered via their init() side effects.
	_ "github.com/denisenkom/go-mssqldb"
	"github.com/go-sql-driver/mysql"
	_ "github.com/lib/pq"

	"github.com/cloudwego/eino/components/tool"
	"github.com/cloudwego/eino/schema"
)

// ExeSQL-specific errors. The previous Phase 3 batch 1 stub returned
// ErrExeSQLDAOMissing because the implementation was a no-op pending
// the (now rejected) GORM wiring. With the real `database/sql`
// implementation in place, the error surface is:
//   - ErrExeSQLNotSelect: SQL failed the SELECT-only safety filter.
//   - ErrExeSQLNoCredentials: the tool has no db_type/host/etc. set
//     (caller forgot to wire the connection params).
//   - ErrExeSQLUnsupportedDB: db_type is one we have not ported yet
//     (trino, IBM DB2).
var (
	ErrExeSQLNotSelect = errors.New(
		"ExeSQL: only SELECT statements are allowed; " +
			"INSERT/UPDATE/DELETE/DDL is rejected for safety",
	)
	ErrExeSQLNoCredentials = errors.New(
		"ExeSQL: connection params not configured (db_type/host/port/database/username/password)",
	)
	ErrExeSQLUnsupportedDB = errors.New(
		"ExeSQL: db_type not yet supported in Go port (trino, IBM DB2 are pending)",
	)
)

const (
	exesqlToolName        = "execute_sql"
	exesqlToolDescription = "This is a tool that can execute SQL."

	exesqlDefaultMaxRecords = 1024
	exesqlDefaultTimeout    = 60 * time.Second
)

// exesqlConnParams captures the user-supplied DB connection details.
// These are tool-level config (set on the canvas node, not exposed
// to the LLM at function-call time), matching the Python ExeSQLParam
// fields. The LLM only sees `sql` and optional `database` in args.
type exesqlConnParams struct {
	DBType     string // mysql | postgres | mariadb | mssql | oceanbase
	Database   string
	Username   string
	Host       string
	Port       int
	Password   string
	MaxRecords int
}

// exesqlArgs is the JSON shape the model sends in. Matches the Python
// ExeSQLParam ToolMeta (sql is required, database is optional).
type exesqlArgs struct {
	SQL      string `json:"sql"`
	Database string `json:"database,omitempty"`
}

// exesqlResult is the JSON envelope returned to the model. The shape
// matches the Python tool's `rows` / `_ERROR` output convention so
// downstream nodes can pattern-match unchanged. `Columns` lets the
// caller preserve order even when `Rows` is empty.
type exesqlResult struct {
	Columns []string         `json:"columns,omitempty"`
	Rows    []map[string]any `json:"rows,omitempty"`
	Error   string           `json:"_ERROR,omitempty"`
}

// exesqlDialer abstracts `sql.Open` for test injection. Production code
// uses defaultExeSQLDialer (== sql.Open). Tests inject a dialer that
// returns a `*sql.DB` backed by DATA-DOG/go-sqlmock so no real DB is
// required.
//
// Returning a *sql.DB (not a Tx) means each statement gets its own
// fresh conn from the pool, matching the Python tool's
// open → execute → close lifecycle.
type exesqlDialer func(driver, dsn string) (*sql.DB, error)

func defaultExeSQLDialer(driver, dsn string) (*sql.DB, error) {
	db, err := sql.Open(driver, dsn)
	if err != nil {
		return nil, err
	}
	// The Python tool does not pool — every call is a fresh connect.
	// Match that: set MaxOpenConns=1 and Close() in InvokableRun so
	// we never leak a connection. Timeouts are honoured by the caller's
	// context.
	db.SetMaxOpenConns(1)
	return db, nil
}

// ExeSQLTool is the Phase 3 batch 1 implementation of the ExeSQL tool.
// It validates SELECT-only at the parser level and executes the
// statement against a user-configured external DB via `database/sql`.
type ExeSQLTool struct {
	conn          exesqlConnParams
	dialer        exesqlDialer
	trustedDialer bool
}

// NewExeSQLTool returns an ExeSQLTool wired to the given connection
// params. The dialer defaults to `sql.Open`; tests can pass a
// sqlmock-backed dialer via WithExeSQLDialer.
func NewExeSQLTool(conn exesqlConnParams) *ExeSQLTool {
	if conn.MaxRecords <= 0 {
		conn.MaxRecords = exesqlDefaultMaxRecords
	}
	return &ExeSQLTool{
		conn:   conn,
		dialer: defaultExeSQLDialer,
	}
}

// WithExeSQLDialer swaps the connection opener. Returns the tool for
// chaining. Used by tests; production code should leave it alone.
func (e *ExeSQLTool) WithExeSQLDialer(d exesqlDialer) *ExeSQLTool {
	if d != nil {
		e.dialer = d
		e.trustedDialer = true
	}
	return e
}

// Info returns the tool's metadata for the chat model. Mirrors the
// Python ExeSQLParam ToolMeta: only `sql` (and optional `database`)
// are visible to the LLM. Connection params are not exposed here —
// they're set on the tool instance, matching the Python convention
// where ExeSQLParam fields like `db_type` / `host` are tool
// configuration, not function-call arguments.
func (e *ExeSQLTool) Info(_ context.Context) (*schema.ToolInfo, error) {
	return &schema.ToolInfo{
		Name: exesqlToolName,
		Desc: exesqlToolDescription,
		ParamsOneOf: schema.NewParamsOneOfByParams(map[string]*schema.ParameterInfo{
			"sql": {
				Type:     schema.String,
				Desc:     "The SQL statement to execute. Must be a SELECT (read-only).",
				Required: true,
			},
			"database": {
				Type:     schema.String,
				Desc:     "Optional target database / schema name. Overrides the tool's configured DB.",
				Required: false,
			},
		}),
	}, nil
}

// InvokableRun validates every statement and opens a fresh connection with
// an enforced read-only transaction. Query failures propagate to the caller.
func (e *ExeSQLTool) InvokableRun(ctx context.Context, argumentsInJSON string, _ ...tool.Option) (string, error) {
	if argumentsInJSON == "" {
		return exesqlErrorResult(errors.New("exesql: empty arguments")), errors.New("exesql: empty arguments")
	}
	var args exesqlArgs
	if err := json.Unmarshal([]byte(argumentsInJSON), &args); err != nil {
		return exesqlErrorResult(fmt.Errorf("exesql: parse arguments: %w", err)),
			fmt.Errorf("exesql: parse arguments: %w", err)
	}
	if strings.TrimSpace(args.SQL) == "" {
		return exesqlErrorResult(errors.New("exesql: empty sql")), errors.New("exesql: empty sql")
	}
	if !isSelectStatement(args.SQL) {
		return exesqlErrorResult(ErrExeSQLNotSelect), ErrExeSQLNotSelect
	}

	// Honor the per-call `database` override if the model supplied one;
	// fall back to the tool's configured DB.
	conn := e.conn
	if args.Database != "" {
		conn.Database = args.Database
	}
	if err := conn.check(); err != nil {
		return exesqlErrorResult(err), err
	}

	driver, dsn, err := exesqlDriverAndDSN(conn)
	if err != nil {
		return exesqlErrorResult(err), err
	}
	if !e.trustedDialer {
		_, ip, err := ResolveAndValidateContext(ctx, "http://"+net.JoinHostPort(conn.Host, strconv.Itoa(conn.Port)))
		if err != nil {
			return exesqlErrorResult(err), err
		}
		conn.Host = ip.String()
		driver, dsn, err = exesqlDriverAndDSN(conn)
		if err != nil {
			return exesqlErrorResult(err), err
		}
	}

	db, err := e.dialer(driver, dsn)
	if err != nil {
		return exesqlErrorResult(fmt.Errorf("exesql: open %s: %w", driver, err)),
			fmt.Errorf("exesql: open %s: %w", driver, err)
	}
	defer db.Close()

	// Apply a wall-clock timeout if the caller did not provide one.
	if _, ok := ctx.Deadline(); !ok {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, exesqlDefaultTimeout)
		defer cancel()
	}

	if err := db.PingContext(ctx); err != nil {
		return exesqlErrorResult(fmt.Errorf("exesql: ping: %w", err)),
			fmt.Errorf("exesql: ping: %w", err)
	}

	tx, err := db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		err = fmt.Errorf("exesql: read-only transaction unavailable: %w", err)
		return exesqlErrorResult(err), err
	}
	defer tx.Rollback()
	res, err := exesqlExecute(ctx, tx, args.SQL, conn.MaxRecords)
	if err != nil {
		return exesqlErrorResult(err), err
	}
	return exesqlMarshalResult(res)
}

// exesqlExecute preserves literal bytes and aborts on any query failure.
func exesqlExecute(ctx context.Context, db *sql.Tx, sqlText string, maxRows int) (*exesqlResult, error) {
	stmts := splitSQLStatements(sqlText)
	res := &exesqlResult{}
	for _, stmt := range stmts {
		stmt = strings.TrimSpace(stmt)
		if stmt == "" {
			continue
		}
		cols, rows, err := exesqlQueryOne(ctx, db, stmt, maxRows)
		if err != nil {
			return nil, fmt.Errorf("exesql: query failed: %w", err)
		}
		if len(res.Columns) == 0 && len(cols) > 0 {
			res.Columns = cols
		}
		res.Rows = append(res.Rows, rows...)
	}
	if len(res.Rows) == 0 {
		// Mirror the Python tool's "no record" sentinel so downstream
		// nodes (VariableAggregator, Message) can match on it.
		// Trigger on zero row data; keep any columns that the first
		// statement populated so the schema survives.
		res.Rows = []map[string]any{{"content": "No record in the database!"}}
	}
	return res, nil
}

// exesqlQueryOne runs a single statement and returns columns + rows.
// Rows are returned as column→value maps with `time.Time` flattened
// to "YYYY-MM-DD" (Python pandas `.dt.strftime('%Y-%m-%d')` parity).
func exesqlQueryOne(ctx context.Context, db *sql.Tx, stmt string, maxRows int) ([]string, []map[string]any, error) {
	rows, err := db.QueryContext(ctx, stmt)
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()

	cols, err := rows.Columns()
	if err != nil {
		return nil, nil, err
	}

	out := make([]map[string]any, 0, 16)
	for rows.Next() {
		if len(out) >= maxRows {
			break
		}
		raw := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range raw {
			ptrs[i] = &raw[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			return nil, nil, err
		}
		row := make(map[string]any, len(cols))
		for i, c := range cols {
			row[c] = exesqlNormalizeCell(raw[i])
		}
		out = append(out, row)
	}
	if err := rows.Err(); err != nil {
		return nil, nil, err
	}
	return cols, out, nil
}

// exesqlNormalizeCell mirrors the Python tool's per-cell conversions:
//   - time.Time -> "YYYY-MM-DD" (Python: df[col].dt.strftime('%Y-%m-%d'))
//   - []byte  -> string (most drivers return text columns as []byte;
//     decoding to string gives JSON-friendly output and matches the
//     Python "df.to_dict(orient='records')" serialization).
//   - nil / NaN / Inf -> JSON null (Python: df.where(pd.notnull(df), None))
//   - everything else passes through unchanged.
func exesqlNormalizeCell(v any) any {
	switch x := v.(type) {
	case nil:
		return nil
	case time.Time:
		return x.Format("2006-01-02")
	case []byte:
		return string(x)
	case float64:
		// JSON does not represent NaN / Inf; the Python tool drops
		// them via `convert_decimals` -> None.
		if isBadFloat(x) {
			return nil
		}
		return x
	}
	return v
}

func isBadFloat(f float64) bool {
	// NaN != NaN; Abs(Inf) > max finite. Avoid importing math.
	if math.IsNaN(f) || math.IsInf(f, 0) {
		return true
	}

	return false
}

// splitSQLStatements splits only outside quotes and comments.
func splitSQLStatements(s string) []string {
	statements, _ := scanSQL(s)
	return statements
}

// stripChunkIDMarkers drops the [ID:123] tokens the RAGFlow chunker
// sometimes embeds in SQL strings (`re.sub(r"\[ID:[0-9]+\]", "", ...)`
// in the Python tool).
var exesqlChunkIDRe = regexp.MustCompile(`\[ID:[0-9]+\]`)

func stripChunkIDMarkers(s string) string { return exesqlChunkIDRe.ReplaceAllString(s, "") }

// exesqlErrorResult returns the JSON envelope for an error path.
func exesqlErrorResult(err error) string {
	b, _ := json.Marshal(exesqlResult{Error: err.Error()})
	return string(b)
}

func exesqlMarshalResult(r *exesqlResult) (string, error) {
	b, err := json.Marshal(r)
	if err != nil {
		return "", fmt.Errorf("exesql: marshal result: %w", err)
	}
	return string(b), nil
}

// exesqlDriverAndDSN maps a (db_type, conn) tuple to the registered
// driver name and DSN. OceanBase reuses the MySQL driver with a
// utf8mb4 charset — same trick the Python tool pulls in
// `pymysql.connect(..., charset='utf8mb4')`.
func exesqlDriverAndDSN(c exesqlConnParams) (driver, dsn string, err error) {
	switch strings.ToLower(c.DBType) {
	case "mysql", "mariadb", "oceanbase":
		config := mysql.NewConfig()
		config.User = c.Username
		config.Passwd = c.Password
		config.Net = "tcp"
		config.Addr = net.JoinHostPort(c.Host, strconv.Itoa(c.Port))
		config.DBName = c.Database
		config.ParseTime = true
		config.Params = map[string]string{"charset": "utf8mb4"}
		return "mysql", config.FormatDSN(), nil
	case "postgres", "postgresql":
		u := url.URL{Scheme: "postgres", User: url.UserPassword(c.Username, c.Password), Host: net.JoinHostPort(c.Host, strconv.Itoa(c.Port)), Path: "/" + c.Database}
		q := url.Values{"sslmode": []string{"disable"}}
		u.RawQuery = q.Encode()
		return "postgres", u.String(), nil
	case "mssql", "sqlserver":
		u := url.URL{Scheme: "sqlserver", User: url.UserPassword(c.Username, c.Password), Host: net.JoinHostPort(c.Host, strconv.Itoa(c.Port))}
		q := url.Values{"database": []string{c.Database}}
		u.RawQuery = q.Encode()
		return "sqlserver", u.String(), nil
	case "trino":
		return "", "", fmt.Errorf("%w: trino", ErrExeSQLUnsupportedDB)
	case "ibm db2":
		return "", "", fmt.Errorf("%w: ibm db2", ErrExeSQLUnsupportedDB)
	default:
		return "", "", fmt.Errorf("ExeSQL: unknown db_type %q", c.DBType)
	}
}

// check returns ErrExeSQLNoCredentials when the required fields are
// missing. Mirrors the Python ExeSQLParam.check() but stripped of
// the UI-specific "empty value" messages.
func (c exesqlConnParams) check() error {
	if c.MaxRecords < 0 || c.MaxRecords > 10000 {
		return fmt.Errorf("ExeSQL: max_records must be in [1,10000]")
	}
	if c.DBType == "" || c.Host == "" || c.Username == "" || c.Database == "" {
		return ErrExeSQLNoCredentials
	}
	if c.Port <= 0 || c.Port > 65535 {
		return fmt.Errorf("ExeSQL: invalid port %d", c.Port)
	}
	return nil
}

// ---------------------------------------------------------------------------
// SELECT-only safety validator
// ---------------------------------------------------------------------------

// leadingKeywordRe matches the first non-comment, non-whitespace keyword
// in a SQL statement. Comments (-- line, /* block */) and string literals
// are stripped before the match.
var leadingKeywordRe = regexp.MustCompile(`^[\s,;(]*([A-Za-z]+)`)

// nonSelectKeywords conservatively rejects mutation and administrative verbs.
var nonSelectKeywords = map[string]struct{}{
	"INSERT": {}, "UPDATE": {}, "DELETE": {}, "REPLACE": {},
	"TRUNCATE": {},
	"CREATE":   {}, "DROP": {}, "ALTER": {}, "RENAME": {},
	"GRANT": {}, "REVOKE": {},
	"LOCK": {}, "UNLOCK": {},
	"CALL": {}, "EXEC": {}, "EXECUTE": {},
	"COPY":   {},
	"VACUUM": {}, "ANALYZE": {},
	"SET": {}, "RESET": {},
	"USE":        {},
	"KILL":       {},
	"LOAD":       {},
	"CHECKPOINT": {},
	"BEGIN":      {}, "COMMIT": {}, "ROLLBACK": {}, "START": {},
	"SHUTDOWN": {},
}

// isSelectStatement checks every lexical statement before the database-level
// read-only transaction provides the final execution boundary.
func isSelectStatement(sql string) bool {
	statements, valid := scanSQL(sql)
	if !valid || len(statements) == 0 {
		return false
	}
	for _, stmt := range statements {
		tokens := strings.FieldsFunc(strings.ToUpper(stripSQLStrings(stripSQLComments(stmt))), func(r rune) bool { return !(r >= 'A' && r <= 'Z') && r != '_' })
		if len(tokens) == 0 {
			return false
		}
		switch tokens[0] {
		case "SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN":
		default:
			return false
		}
		for _, token := range tokens {
			if _, bad := nonSelectKeywords[token]; bad {
				return false
			}
			if token == "INTO" || token == "OUTFILE" || token == "DUMPFILE" || token == "FOR" || token == "PRAGMA" || token == "ANALYZE" {
				return false
			}
		}
	}
	return true
}

// stripSQLComments removes -- line comments and /* ... */ block comments.
// We don't try to handle nested comments (MySQL/PG/SQLite differ) — this
// is a best-effort guard for the SELECT validator, not a SQL parser.
func stripSQLComments(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	i := 0
	for i < len(s) {
		if i+1 < len(s) && s[i] == '-' && s[i+1] == '-' {
			for i < len(s) && s[i] != '\n' {
				i++
			}
			continue
		}
		if i+1 < len(s) && s[i] == '/' && s[i+1] == '*' {
			i += 2
			for i+1 < len(s) && !(s[i] == '*' && s[i+1] == '/') {
				i++
			}
			i += 2
			continue
		}
		b.WriteByte(s[i])
		i++
	}
	return b.String()
}

// stripSQLStrings removes single- and double-quoted string literals and
// replaces them with empty placeholders so that keywords inside string
// contents don't confuse the validator.
func stripSQLStrings(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	i := 0
	inStr := byte(0)
	for i < len(s) {
		c := s[i]
		if inStr != 0 {
			if c == inStr {
				if i+1 < len(s) && s[i+1] == inStr {
					b.WriteByte(' ')
					i += 2
					continue
				}
				inStr = 0
			}
			b.WriteByte(' ')
			i++
			continue
		}
		if c == '\'' || c == '"' || c == '`' {
			inStr = c
			b.WriteByte(' ')
			i++
			continue
		}
		b.WriteByte(c)
		i++
	}
	return b.String()
}

// scanSQL preserves the exact statement bytes while recognizing separators
// outside quoted literals and comments. Unsupported/unclosed lexical forms
// fail validation before any database connection is opened.
func scanSQL(s string) ([]string, bool) {
	var statements []string
	start := 0
	var quote byte
	line, block := false, false
	for i := 0; i < len(s); i++ {
		c := s[i]
		if line {
			if c == '\n' {
				line = false
			}
			continue
		}
		if block {
			if i+1 < len(s) && c == '*' && s[i+1] == '/' {
				block = false
				i++
			}
			continue
		}
		if quote != 0 {
			if c == '\\' {
				if i+1 < len(s) {
					i++
				}
				continue
			}
			if c == quote {
				if i+1 < len(s) && s[i+1] == quote {
					i++
					continue
				}
				quote = 0
			}
			continue
		}
		if c == '$' || c == '#' {
			return nil, false
		}
		if c == '\'' || c == '"' || c == '`' {
			quote = c
			continue
		}
		if i+1 < len(s) && c == '-' && s[i+1] == '-' {
			line = true
			i++
			continue
		}
		if i+1 < len(s) && c == '/' && s[i+1] == '*' {
			block = true
			i++
			continue
		}
		if c == ';' {
			if stmt := strings.TrimSpace(s[start:i]); stmt != "" {
				statements = append(statements, stmt)
			}
			start = i + 1
		}
	}
	if quote != 0 || block {
		return nil, false
	}
	if stmt := strings.TrimSpace(s[start:]); stmt != "" {
		statements = append(statements, stmt)
	}
	return statements, true
}
