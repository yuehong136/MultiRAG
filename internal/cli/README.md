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

-- Admin mode: ingestion task summaries (read-only)
LIST TASKS;
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

## Skills asset library

`skills help` lists the standalone asset commands (`multirag-assets-v1`). These
commands use the explicit `/api/v1/skill-assets` namespace. Upgrade the backend
to expose that namespace before upgrading the CLI. `/api/v1/skills` is a deployment
alias and is never probed to guess its protocol. Python and Go own independent
data; the legacy Go asset protocol may be read-only during source convergence.
The first release accepts local directories and ZIP archives and never executes
package code. It does not connect the existing MCP tool selector to skills.

```bash
./multirag_cli -t "$MULTIRAG_API_TOKEN" skills create-space "Team skills"
./multirag_cli -t "$MULTIRAG_API_TOKEN" skills install SPACE_ID ./my-skill my-skill 1.0.0 --activate --key install-1
./multirag_cli -t "$MULTIRAG_API_TOKEN" skills wait OPERATION_ID
./multirag_cli -t "$MULTIRAG_API_TOKEN" skills list SPACE_ID
./multirag_cli -t "$MULTIRAG_API_TOKEN" skills download SPACE_ID VERSION_ID ./skill-1.0.0.zip
```

Commands print JSON with decimal-string model IDs. Long operations print their
idempotency key to stderr before sending and return an accepted operation ID;
acceptance is not successful completion. `wait` returns nonzero for failed or
partial operations. After an uncertain request, reuse `--key` or inspect the
operation; synchronous creates require a list/readback before retrying.
Downloads publish only complete bytes and refuse to overwrite existing files.

Configure exact embedding/rerank model IDs using `skills models`, `skills config
SPACE_ID`, and `skills set-config SPACE_ID config.json` (include the current
`revision`). Explicit `skills reindex SPACE_ID` publishes the new configuration
only after index verification. Search supports keyword/vector/hybrid; empty
queries require keyword mode. See [the shared contract](../../docs/skills/CONTRACT.md).

## Skills core

`skill-core` and the Skills context provider use the fixed `/api/v1/skill-core`
protocol on either backend. Every operation names a real space; there is no
implicit default. IDs are decimal strings for models and opaque strings for
spaces. Use an existing unique space name or its ID in context paths.

```bash
./multirag_cli -t "$MULTIRAG_API_TOKEN" skill-core create-space "Team skills"
./multirag_cli -t "$MULTIRAG_API_TOKEN" skill-core set-config SPACE_ID config.json
./multirag_cli -t "$MULTIRAG_API_TOKEN" install-skill SPACE_ID ./my-skill --version 1.0.0
./multirag_cli -t "$MULTIRAG_API_TOKEN" ls skills/SPACE_ID
./multirag_cli -t "$MULTIRAG_API_TOKEN" cat skills/SPACE_ID/my-skill/SKILL.md
./multirag_cli -t "$MULTIRAG_API_TOKEN" skill-core search SPACE_ID "matching query"
./multirag_cli -t "$MULTIRAG_API_TOKEN" skill-core reindex SPACE_ID
./multirag_cli -t "$MULTIRAG_API_TOKEN" uninstall-skill SPACE_ID my-skill
```

Core installation accepts a local directory with a root SKILL.md; remote sources,
force replacement and ZIP installation belong to separate commands or future
scope. Core versions are directories: reindex selects the highest numeric
three-part version. No immutable version ID or operation ID is fabricated.
Listing uses the complete Files tree even before indexing, with pagination.
An upload followed by indexing failure returns an error explaining that files
remain saved and reindex is needed. Uninstall issues one Files delete request;
the backend coordinates index cleanup. A delete-space 202 remains pending until
the space becomes unavailable. See [the core contract](../../docs/skills/CORE_CONTRACT.md).

`LIST TASKS;` requires `--admin` and an authenticated administrator session.
It calls `GET /api/v1/admin/tasks`; user mode rejects this command. Empty task
lists return an empty array. `chunk_count` counts whitespace-separated chunk IDs,
including zero for missing IDs. Task logs and chunk contents are not included.

SQL dataset search (`SEARCH 'question' ON DATASETS 'name1,name2';`) and its sequential/concurrent
benchmark modes call `POST /api/v1/datasets/search` with `dataset_ids` and Bearer
authentication. Dataset names resolve through `GET /api/v1/datasets` first.

## Media commands

`ASR WITH 'model@instance@provider' AUDIO './speech.wav';`,
`TTS WITH 'model@instance@provider' TEXT 'hello';` and
`OCR WITH 'model@instance@provider' FILE './image.png';` use the protected media APIs.
OCR also accepts `URL 'https://example.com/image.png'` instead of `FILE`; the provider
fetches the image. Gitee supports both inputs. File input is limited to 20 MiB and
media calls to 120 seconds. Drivers without the requested capability return code 501.
ASR/TTS accept a `STREAM` prefix. `CHAT ... AUDIO './speech.wav';` sends base64 audio.
Files are bounded to 20 MiB; audio input supports WAV/MP3. TTS returns base64 audio,
including under `-o json`, and does not automatically write a local file.

Built-in media inference drivers currently return **501 unsupported**. A catalog
entry alone does not enable a driver capability. Missing results, provider errors,
failed/truncated streams and nonzero business codes fail the CLI command. See the
[Go media contract](../../docs/ragflow-porting/go-media.md) for payloads and the
controlled-adapter verification boundary.

An explicit empty `text` is valid for silent audio or a blank page; missing/null
text and streams without any result event fail.
