"""Live check of the model gateway against a real OpenAI-protocol model service.

Not collected by pytest. The script creates a scratch database, registers the
model named by the environment, publishes it, issues a key, and calls it with
the OpenAI SDK through the gateway. The scratch database, which briefly holds
the upstream credential, is dropped at the end. No credential is printed.

    MODEL_GATEWAY_LIVE_API_KEY=... MODEL_GATEWAY_LIVE_CHAT_MODEL=<model> \
        uv run python tests/manual/model_gateway_live.py

Optional: MODEL_GATEWAY_LIVE_FACTORY (default ``OpenAI``),
MODEL_GATEWAY_LIVE_BASE_URL (default: the provider's own default) and
MODEL_GATEWAY_LIVE_EMBEDDING_MODEL.
"""

import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import openai
from sqlalchemy.orm import Session

from tests.support.database import scratch_database
from tests.support.model_gateway import MANAGEMENT_PREFIX, data_plane, logs, management, register_model, sdk
from tests.support.provider_instances import tenant

_PROMPT = [{"role": "user", "content": "Reply with the single word: pong"}]


def main() -> int:
    api_key = os.environ.get("MODEL_GATEWAY_LIVE_API_KEY", "")
    chat_model = os.environ.get("MODEL_GATEWAY_LIVE_CHAT_MODEL", "")
    if not api_key or not chat_model:
        print("Set MODEL_GATEWAY_LIVE_API_KEY and MODEL_GATEWAY_LIVE_CHAT_MODEL.", file=sys.stderr)
        return 2
    factory = os.environ.get("MODEL_GATEWAY_LIVE_FACTORY", "OpenAI")
    base_url = os.environ.get("MODEL_GATEWAY_LIVE_BASE_URL", "")
    embedding_model = os.environ.get("MODEL_GATEWAY_LIVE_EMBEDDING_MODEL", "")
    owner = uuid4().hex
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as evidence, scratch_database(Path(evidence)) as engine:
        with Session(engine) as db:
            db.add(tenant(owner))
            registered = {"live-chat": (register_model(db, owner, factory, chat_model, "chat", base_url, api_key), "chat")}
            if embedding_model:
                registered["live-embedding"] = (register_model(db, owner, factory, embedding_model, "embedding", base_url, api_key), "embedding")
            db.commit()

        with management(engine, owner) as admin:
            published = []
            for name, (model_id, model_type) in registered.items():
                response = admin.post(f"{MANAGEMENT_PREFIX}/models", json={"name": name, "model_id": model_id, "model_type": model_type})
                if response.status_code != 200:
                    print(f"publish {name}: HTTP {response.status_code} {response.json().get('message')}", file=sys.stderr)
                    return 1
                published.append(response.json()["data"]["id"])
            gateway_key = admin.post(f"{MANAGEMENT_PREFIX}/keys", json={"name": "live-check", "allowed_model_ids": published}).json()["data"]["key"]

        with data_plane(engine, None) as gateway:
            client = sdk(gateway, gateway_key)
            print("models:", [model.id for model in client.models.list().data])
            try:
                completion = client.chat.completions.create(model="live-chat", messages=_PROMPT)
                print(f"chat: model={completion.model!r} content={(completion.choices[0].message.content or '')[:80]!r} usage={completion.usage.model_dump() if completion.usage else None}")
                chunks = list(client.chat.completions.create(model="live-chat", messages=_PROMPT, stream=True))
                text = "".join(chunk.choices[0].delta.content or "" for chunk in chunks if chunk.choices)
                usage_only = sum(1 for chunk in chunks if not chunk.choices)
                print(f"stream: chunks={len(chunks)} content={text[:80]!r} usage_only_chunks_seen_by_caller={usage_only}")
                if usage_only:
                    failures.append("the caller received a usage chunk it did not ask for")
                if embedding_model:
                    embedding = client.embeddings.create(model="live-embedding", input="model gateway live check")
                    print(f"embedding: model={embedding.model!r} dimensions={len(embedding.data[0].embedding)} usage={embedding.usage.model_dump()}")
            except openai.APIStatusError as error:
                failures.append(f"gateway returned HTTP {error.status_code} code={error.code!r}: {error.message}")

        rows = logs(engine)
        print(f"request logs: {len(rows)}")
        for row in rows:
            print(
                f"  {row.endpoint} stream={row.stream} status={row.status_code} error={row.error_type}/{row.error_code} upstream_model={row.upstream_model_name!r} "
                f"tokens={row.prompt_tokens}/{row.completion_tokens}/{row.total_tokens} usage_source={row.usage_source} first_token_ms={row.first_token_ms} duration_ms={row.duration_ms}"
            )
            if row.status_code != 200 or row.error_type is not None:
                failures.append(f"{row.endpoint} was logged with status {row.status_code}")
            elif row.usage_source != "reported":
                failures.append(f"{row.endpoint} has no reported usage")
        if any(api_key in str(row.to_dict()) for row in rows):
            failures.append("the upstream credential appeared in a request log")

    for failure in failures:
        print("FAILED:", failure, file=sys.stderr)
    print("result:", "FAILED" if failures else "OK")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
