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

### Provider credential checks

User mode can check a provider without saving an instance:

```sql
CHECK PROVIDER 'vllm' REGION 'default' KEY '' URL 'http://localhost:8000/v1';
```

Region, key and optional base URL reach the Go provider connection endpoint.
Unknown regions fail. Credential commands are omitted from REPL history, and
single-command failures return a nonzero exit without displaying provider bodies
or credentials. A terminating semicolon is required by the parser; single-command
mode follows its existing automatic semicolon behavior.

### Dataset chunks and metadata

User-mode chunk reads and updates require the dataset and document identity.
Dataset references may be UUID1 IDs or exact, unambiguous names. Statements use
a terminating semicolon; single-command mode adds one when omitted.

```sql
GET CHUNK 'chunk-id' OF DOCUMENT 'document-id' IN DATASET 'knowledge';
GET METADATA OF DATASET 'knowledge' 'another dataset';
UPDATE CHUNK 'chunk-id' OF DOCUMENT 'document-id' IN DATASET 'knowledge' SET '{"content":"updated","available":false}';
REMOVE TAGS 'obsolete', 'old' FROM DATASET 'knowledge';
REMOVE CHUNKS 'chunk-one' 'chunk-two' FROM DOCUMENT 'document-id' IN DATASET 'knowledge';
DELETE METADATA OF DOCUMENT 'document-id' KEYS '["obsolete"]';
CREATE CHUNK STORE FOR DATASET 'knowledge' VECTOR SIZE 384;
DROP CHUNK STORE FOR DATASET 'knowledge';
CREATE METADATA STORE;
DROP METADATA STORE;
INSERT CHUNKS FROM FILE 'chunks.json';
```

`GET CHUNK` uses the dataset/document path and includes disabled chunks while
retrieval retains its availability filter. Metadata requires authorization for
every requested dataset and returns field/value/document mappings; `-o json`
preserves this object, while table/plain output lists its values deterministically.
Chunk IDs in `REMOVE CHUNKS` are space separated. The dataset/document URL scope
is authoritative. `DELETE METADATA` accepts distinct non-empty literal keys;
`KEYS '[]'`, null, empty strings and duplicates are rejected. Omit `KEYS` to delete
that document's entire metadata row. Removing its last key also removes the row;
removing absent keys leaves it unchanged. SQL dataset access is required before
reading or writing the document store.

Store commands use `/api/v1/tenant/chunk_store` and `metadata_store`. ES chunk
stores share a tenant index, so dropping one removes only that dataset's chunks.
Metadata store commands address the authenticated user's own tenant. File imports
remain local Go management operations: paths are on the server, and every imported
dataset/document must belong to the authorized namespace. Both `index_name` and
`table_name` identify the same server-derived base name; neither may override it.
The old `DATASET TABLE` / `METADATA TABLE` grammar and Go management route names
have been retired with their CLI consumers. Tag removal uses the dataset
REST endpoint and validates the full tag list before writing. Store errors are
returned to the caller; multiple tag removals may partly complete on a later
storage failure, so retrying the same tags is supported.

For a configured local document-parser provider, model declarations accept
`DOC_PARSE`. File submission and task polling keep the standard syntax:

```sql
ADD MODEL 'pipeline' TO PROVIDER 'mineru_local' INSTANCE 'one' WITH DOC_PARSE TOKENS 8192;
PARSE WITH 'pipeline@one@mineru_local' FILE './document.pdf';
SHOW 'one@mineru_local' TASK 'task-id';
```

The local parser requires a configured instance URL. Submission returns a pending
task; poll the task for completed segments. Its URL submission and task listing
capabilities remain unavailable.

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
Multiple names may be quoted separately (`'name1' 'name2'`) or supplied in one
comma-separated quoted string. A semicolon terminates the statement.

```sql
SEARCH 'question' ON DATASETS 'knowledge' WITH page 1 page_size 20 top_k 0 similarity_threshold 0.0 vector_similarity_weight 0.3 keyword false doc_ids [] cross_languages [];
SEARCH 'question' ON DATASETS 'knowledge' WITH tenant_rerank_id 42;
```

`WITH` options are space-separated. `page` and `page_size` are positive;
`top_k` is nonnegative (`0` means no limit). Similarity threshold and vector
weight accept finite numbers in `[0,1]`. `keyword` and `use_kg` accept booleans.
`doc_ids` and `cross_languages` accept bracketed quoted string lists, preserving
an explicit empty list. `doc_ids []` asks for an empty document scope; omission
leaves document selection to the server. `tenant_rerank_id` is a positive numeric
model ID; `rerank_id` is a model selector, and the two cannot be combined.
`meta_data_filter` takes a quoted JSON object; `search_id` selects saved configuration.
Unsupported server combinations fail explicitly. Invalid, duplicate or unknown
options fail before dataset lookup. All benchmark modes use the same payload.

Custom models can be added or removed in one atomic request:

```sql
ADD MODEL 'chat-model embed-model' TO PROVIDER 'vllm' INSTANCE 'local' WITH CHAT VISION THINK TOKENS 0, EMBEDDING;
REMOVE MODEL 'chat-model embed-model' FROM PROVIDER 'vllm' INSTANCE 'local';
EMBED TEXT 'hello' WITH 'embed-model@local@vllm';
EMBED TEXT 'hello' WITH 'embed-model@local@vllm' DIMENSION 0;
```

Each whitespace-separated model name has one comma-separated option group.
`TOKENS` is optional and defaults to explicit `0`; repeated options and duplicate
names are rejected. Single-model syntax follows the same batch path. `DROP MODEL`
also deletes a batch. Model or instance identifiers used by other commands retain
their existing numeric and composite selector contracts. Embedding `DIMENSION`
may be omitted; explicit `0` is sent to request the provider default dimension.

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

MiniMax supports TTS and streaming TTS; FishAudio supports TTS, streaming TTS and
ASR; OpenRouter supports TTS. FishAudio streaming ASR and OpenRouter streaming TTS
return **501 unsupported**. Gitee implements OCR for file and URL inputs. A catalog
entry alone does not enable a driver capability. Missing results, provider errors,
failed/truncated streams and nonzero business codes fail the CLI command. See the
[Go media contract](../../docs/ragflow-porting/go-media.md) for payloads and the
controlled-adapter verification boundary.

An explicit empty `text` is valid for silent audio or a blank page; missing/null
text and streams without any result event fail.


## LocalAI and Voyage AI

Use the canonical provider names `LocalAI` and `Voyage AI`. LocalAI instances need
an explicit tenant base URL (for example `http://127.0.0.1:8080/v1`); their API key
is optional. The Go driver supports history chat, SSE, embeddings, reranking,
model discovery and connection checks. The three reasoning response fields
`reasoning_content`, `reasoning` and `thinking` are read in that order.

Voyage AI supports embeddings and reranking only. It sends `output_dimension`
and `top_k` on those endpoints. Discovery and connection checks return an error
without spending quota on an inference probe. Neither provider declares a Go
media capability. Provider HTTP acceptance with controlled responses establishes
the adapter contract; live provider quality and production behavior need separate
verification.

ASR and TTS accept `PARAM` with a quoted JSON object (up to 64 KiB). The object is
sent as `asr_config.params` or `tts_config.params`; model identity, credentials,
input bytes/text and streaming cannot be overridden through it. ASR reads the
client file and uploads its bytes, never a path for the server to open.

```sql
TTS WITH 'speech-2.8-hd@voice@MiniMax' TEXT 'hello' PARAM '{"voice_setting":{"voice_id":"English_radiant_girl","speed":1},"audio_setting":{"sample_rate":32000}}' FORMAT wav SAVE './audio';
STREAM TTS WITH 's2-pro@voice@FishAudio' TEXT 'hello' PARAM '{"reference_id":"voice-id"}' FORMAT mp3 PLAY;
ASR WITH 'transcribe-1@voice@FishAudio' AUDIO './speech.wav' PARAM '{"language":"en","ignore_timestamps":true}';
```

TTS `FORMAT` overrides the format in provider params and preserves other nested
settings; otherwise known format fields in params apply, with MP3 as the default.
`SAVE 'directory'` creates a private audio file in that directory and returns
`saved_path`. Filenames are sanitized and unique, including models containing `/`.
`PLAY` explicitly invokes macOS `afplay`, or installed `mpv`/`ffplay`; player failure
fails the command. PLAY alone uses a temporary file and removes it after playback.
Without SAVE or PLAY the command returns audio without creating a local file.

MiniMax streams require successful business codes and final `data.status=2`;
repeated final aggregate audio is validated and not appended twice. FishAudio's
SSE endpoint uses a clean EOF after complete events. The API emits its own done
only after provider completion; malformed chunks, transport errors, partial SSE
frames, cancellation and failed senders remain errors. These adapters were verified
with controlled provider HTTP, not real supplier credentials.


### Admin system variables

In `--admin` mode, use `LIST VARS;`, `SHOW VAR mail.;`, and
`SET VAR mail.port 0;`. `SET VAR mail.use_tls false;` preserves false, and
`SET VAR mail.password '';` writes an explicit empty string. `SHOW VAR` sends
`var_name` as a GET query parameter and uses exact-name priority before literal
prefix matching. Invalid values and denied requests return a nonzero process exit.
The [variable types and examples](../../admin/client/README.md#系统变量admin)
apply to both CLI implementations.


`LIST DOCUMENTS FROM '<dataset-id-or-name>';` returns the authorized document page
with token/chunk counts, parser configuration, pipeline/author names and timestamps.
Run states use `UNSTART`, `RUNNING`, `CANCEL`, `DONE`, and `FAIL`. Documents without
file links remain visible, and multiple file links do not duplicate a document.

## Bedrock and FuturMix

Bedrock instances may omit `URL`: runtime and control endpoints are derived from
`bedrock_region` in the credential JSON. An explicit `REGION` overrides that value;
`default` keeps the credential region. A URL override applies to both runtime and
model discovery for that instance. The instance name is never used as an AWS
region. For example:

```sql
CREATE PROVIDER 'Bedrock' INSTANCE 'office' KEY '{"auth_mode":"access_key_secret","bedrock_ak":"YOUR_ACCESS_KEY","bedrock_sk":"YOUR_SECRET_KEY","bedrock_region":"us-east-1"}';
CHAT WITH 'anthropic.claude-3-5-sonnet-20241022-v2:0@office@Bedrock' MESSAGE 'hello';
```

The Go Bedrock driver supports text Converse and binary streaming. A single
`MESSAGE` uses scalar text; media and repeated content parts retain structured
payloads and require a provider that supports them. Native credentials and endpoint
selection remain bound to each SQL instance, including the existing two-part
legacy model reference path.

FuturMix instance creation saves its configuration without an inference probe.
`LIST MODELS FROM 'FuturMix' 'office';` reads the static catalog with normal API
authentication. Dynamic `LIST SUPPORTED MODELS` and `CHECK INSTANCE` remain
unsupported and do not call an undocumented endpoint or spend inference quota.
