# MultiRAG Go CLI

The CLI uses the frozen upstream parser, command files and `filesystem/` route.
Python remains the production backend; a successful CLI build does not validate
Python, Web or real provider credentials. Build and native prerequisites are in
[the development guide](../../docs/development.md#go-并行实现的本地验证).

```sh
./bin/multirag_cli --help
./bin/multirag_cli -h 127.0.0.1:9384 -t '<owned-api-token>' 'LIST DATASETS;'
./bin/multirag_cli --admin -h 127.0.0.1:9383 'PING;'
```

API mode is the default. `--admin` selects the separate admin connection.
Connection options include `-h/--host`, `-u/--user`, `-p/--password`,
`-t/--token`, API-mode `-f/--config`, and `-o/--output`.
Use either an API token or username/password for one API profile. Admin mode
rejects API token and API configuration flags. Single SQL-like commands are
quoted as one shell argument; the parser rejects trailing second statements.
Interactive mode uses `\?` for help and `\q` to exit.

The API configuration file uses top-level `host`, `api_token`, `user_name` and
`password`, plus named profiles under `api_servers`. Keep private configuration
outside the repository with mode `0600`. Login encryption reads
`configs/go/public.pem` through the original project-directory lookup; private keys
and login credentials are not repository assets. History uses
`~/.multirag_cli_history` in interactive mode. Single-command execution does not
write interactive history.

Named API profiles retain their own host and credentials. Profile inspection
redacts credential values. `SAVE CONFIG` is explicitly unsupported by the frozen
implementation; it does not report success without writing a file. See
`parser.go`, `user_parser.go` and `admin_parser.go` for exact commands and
mode restrictions, rather than relying on earlier CLI aliases or private DTOs.

Model discovery calls the original typed driver inventory. Tenant model IDs are
UUIDs; the separate legacy TenantLLM numeric ID is not an inference model ID.
Embedding results contain input `index` and vector `embedding`; driver-specific
media, dimension and streaming contracts remain in their corresponding models.
HTTP failures, malformed completion envelopes and missing required business
codes fail explicitly. Binary/file responses retain their original bytes.
Streaming uses the original incremental response path. Supplier execution has
only been validated against controlled origins unless a receipt states otherwise.

The filesystem commands use `filesystem/` and its registered nodes. SkillHub
local sources are scanned and uploaded through the canonical folder, multipart
file and index APIs. Invalid manifests, escaping paths and index/upload failures
are errors; no immutable worker/package or old managed-asset gateway is kept.

Local ingestion commands run without login or a server:

```sql
PARSE FILE 'document.docx';
PARSE FILE 'document.xlsx' WITH 'parser.json';
CHUNK 'document.txt' WITH 'pipeline.json';
EXPLAIN CHUNK 'document.txt' WITH 'pipeline.json';
```

Office parsing uses the default native office_oxide route. Parser configuration
is a JSON string map such as `{"lib_type":"office_oxide"}`. There is no builtin
OOXML backend or optional Office build tag. The original parser prints extracted
text and returns an error on failure. DOC/DOCX/PPT/PPTX/XLS/XLSX require real native
fixtures for acceptance; PDF is explicitly unimplemented. EXPLAIN compiles the
DSL without opening the input document. Repeated operators retain their declared
order. See [ingestion](../ingestion/README.md) for the actual worker boundary.

Tests run through `scripts/run_registered.py` with owned SQL, Redis, object,
HTTP and filesystem resources. `MULTIRAG_M_CLI` selects the candidate binary for
actual subprocess consumers, and `MULTIRAG_OFFICE_FIXTURES` selects the registered
six-format native fixture directory. Missing required services are failures.
