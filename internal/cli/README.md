# MultiRAG CLI (Go Version)

This is the Go implementation of the MultiRAG command-line interface, compatible with the Python version's syntax.

## Features

- Interactive mode and single command execution
- Full compatibility with Python CLI syntax
- Recursive descent parser for SQL-like commands
- Context Engine (virtual filesystem) for intuitive resource management
- Multiple output formats: table (default), plain, json
- Support for all major commands:
  - User management: LOGIN, LOGOUT, REGISTER, CREATE USER, DROP USER, LIST USERS, etc.
  - Service management: LIST SERVICES, SHOW SERVICE, STARTUP/SHUTDOWN/RESTART SERVICE
  - Role management: CREATE ROLE, DROP ROLE, LIST ROLES, GRANT/REVOKE PERMISSION
  - Dataset management via Context Engine: `ls`, `search`, `cat`
  - Model management: SET/RESET DEFAULT LLM/VLM/EMBEDDING/etc.
  - And more...

## Usage

### Build and run

```bash
go build -o multirag_cli ./cmd/multirag_cli.go

# Interactive REPL (user mode by default; --admin for admin mode)
./multirag_cli

# Single command (SQL, use quotes)
./multirag_cli -t <api-token> "LIST DATASETS"

# Single command (Context Engine, no quotes)
./multirag_cli -t <api-token> ls datasets
```

Connection flags: `-h host[:port]`, `-u/--user`, `-p/--password`, `-t/--token`,
`-f/--config <multirag.yml>`, `-o/--output table|plain|json`, `--admin`.

## Architecture

```
internal/cli/
├── cli.go              # REPL, single-command mode, ParseConnectionArgs, executeContextEngine
├── client.go           # MultiRAGClient + ExecuteCommand dispatch + httpClientAdapter
├── http_client.go      # HTTP client for API communication
├── lexer.go            # Lexical analyzer
├── parser.go           # Recursive descent parser (shared leaves)
├── admin_parser.go     # Admin command grammar
├── user_parser.go      # User command grammar
├── common_command.go   # Commands shared by admin/user (login, logout, ping, ...)
├── admin_command.go    # Admin command executors
├── user_command.go     # User command executors
├── response.go         # ResponseIf types (table/plain/json rendering)
├── table.go            # PrintTableSimpleByFormat
└── contextengine/      # Context Engine (virtual filesystem)
    ├── engine.go       # Core engine: path resolution, command routing
    ├── types.go        # Node, Command, Result types
    ├── provider.go     # Provider interface + path helpers
    ├── dataset_provider.go  # Dataset provider (datasets / documents / retrieval)
    ├── file_provider.go     # File manager provider
    └── utils.go        # Helper functions
```

## Context Engine

The Context Engine provides a unified virtual filesystem over MultiRAG's existing
RESTful APIs. All logic is client-side; no server changes are required.

### Supported paths

| Path | Description |
|------|-------------|
| `datasets` | List all datasets |
| `datasets/{name}` | List documents in a dataset |
| `datasets/{name}/{doc}` | Document info |
| `{folder}` / `files/{folder}` | File manager folders/files |

### Commands

#### `ls [path] [-n limit]` — list nodes

```bash
ls                              # List root (providers and file_manager folders)
ls datasets                     # List all datasets
ls datasets/kb1                 # List documents in dataset kb1
ls datasets -n 50               # List 50 datasets
```

#### `search -q <query> [options]` — semantic search in datasets

Options: `-d/--dir <path>` (repeatable), `-q/--query <query>` (required),
`-k/--top-k <n>` (default 10), `-t/--threshold <0.0-1.0>` (default 0.2).
Output defaults to JSON; use `-o plain` / `-o table`.

```bash
search -q "machine learning"                    # Search all datasets
search -d datasets/kb1 -q "neural networks"     # Search in kb1
search -q "RAG" -k 20 -t 0.5                     # 20 results, threshold 0.5
```

#### `cat <path>` — show a text file's content

```bash
cat files/docs/notes.md          # Show file content
cat datasets/kb1/document.pdf     # Error: not a text file / not yet supported
```

## Command Examples

```sql
-- Authentication
LOGIN USER 'admin@example.com';
LOGOUT;

-- User management
REGISTER USER 'john' AS 'John Doe' PASSWORD 'secret';
LIST USERS;
SHOW USER 'john';

-- Service management
LIST SERVICES;
SHOW SERVICE 1;
PING;

-- Dataset / model management
LIST DATASETS;
SET DEFAULT LLM 'gpt-4';
RESET DEFAULT LLM;

-- Context Engine (no quotes, no semicolon)
ls;                                       -- List root
ls datasets;                              -- List datasets
ls datasets/my_dataset;                   -- List documents in a dataset
search -d datasets/my_dataset -q "test";  -- Search in a dataset

-- Meta commands
\?          -- Show help
\q          -- Quit
\format json -- Set output format
\c          -- Clear screen
```

## Parser Implementation

The parser uses a hand-written recursive descent approach instead of go-yacc for:
- Better control over error messages
- Easier to extend and maintain
- No code generation step required

The parser structure follows the grammar defined in the Python version, ensuring full syntax compatibility.
