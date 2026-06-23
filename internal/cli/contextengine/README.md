# ContextFS - Context Engine File System

ContextFS is a context engine interface for MultiRAG, providing a Unix-like
filesystem view over datasets, documents and files through the existing RESTful
APIs. All logic runs client-side in the CLI; no server-side changes are needed.

## Directory Structure

```
user_id/
├── datasets/
│   └── my_dataset/
│       └── ...
├── tools/
│   ├── registry.json
│   └── tool_name/
│       ├── DOC.md
│       └── ...
├── skills/
│   ├── registry.json
│   └── skill_name/
│       ├── SKILL.md
│       └── ...
└── memories/
    └── memory_id/
        ├── sessions/
        │   ├── messages/
        │   ├── summaries/
        │   │   └── session_id/
        │   │       └── summary-{datetime}.md
        │   └── tools/
        │       └── session_id/
        │           └── {tool_name}.md          # User level of memory on Tools usage
        ├── users/
        │   ├── profile.md
        │   ├── preferences/
        │   └── entities/
        └── agents/
            └── agent_space/
                ├── tools/
                │   └── {tool_name}.md          # Agent level of memory on Tools usage
                └── skills/
                    └── {skill_name}.md          # Agent level of memory on Skills usage
```

## Supported Commands

- `ls [path]` - List directory contents
- `cat <path>` - Display file contents (only for text files)
- `search <query>` - Semantic search

## Design

1. **No server-side changes** — all logic implemented client-side using existing APIs.
2. **Provider pattern** — modular providers for different resource types (datasets, files).
3. **Unified interface** — common `ls` / `search` / `cat` across providers.
4. **Path-based navigation** — virtual paths like `datasets`, `datasets/{name}`, `files/{folder}`.

## Providers

| Provider | Root | Backing APIs |
|----------|------|--------------|
| Dataset  | `datasets` | `GET /api/v1/datasets`, `GET /api/v1/datasets/{id}/documents`, `POST /v1/chunk/retrieval_test` |
| File     | `files`    | `GET /api/v1/files`, `GET /api/v1/files/{id}` |

> Note: the retrieval request uses the `kb_ids` field to match MultiRAG's
> `RetrievalTestRequest` (RAGFlow's web handler reads `kb_id`).
