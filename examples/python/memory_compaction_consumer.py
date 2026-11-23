"""Explicit external caller: fresh authorized preview, optional single OpenAI call.

Run from the checkout: python -m examples.python.memory_compaction_consumer ...
MULTIRAG_API_BASE includes /api/v1. API credentials are separate from the model
key. Preview prints only bindings/budget, never API credentials or source bodies.
This example is not the server's LLMBundle adapter or a context application API.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit

import httpx
from openai import OpenAI

from common.token_utils import encoder
from memory.compaction.consumption import consumption_reference, prepare_compaction_request


def consume(
    reference: dict[str, str],
    *,
    api_base: str,
    api_token: str,
    scope: dict[str, str | None],
    model: str,
    question: str,
    context_window_tokens: int,
    output_reserve_tokens: int = 512,
    safety_margin_tokens: int = 256,
    send: bool = False,
    model_key: str | None = None,
    count_tokens: Callable[[str], int] | None = None,
) -> dict[str, Any]:
    """Never fall back to old RAW, regenerate a summary or retry a paid call."""
    ref = consumption_reference(reference)
    parsed = urlsplit(api_base)
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
        or (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}))
    ):
        raise ValueError("invalid_compaction_api_base")
    if not api_token or type(send) is not bool:
        raise ValueError("invalid_compaction_consumer_credentials")
    if send and not model_key:
        raise ValueError("openai_model_key_required")
    with httpx.Client(timeout=30, follow_redirects=False) as api:

        def fetch(memory_id: str, operation_id: str) -> Any:
            url = api_base.rstrip("/") + "/memories/" + quote(memory_id, safe="") + "/context-compaction-operations/" + quote(operation_id, safe="")
            response = api.get(url, headers={"Authorization": "Bearer " + api_token, "Cache-Control": "no-store"})
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict) or value.get("retcode") != 0 or type(value.get("retcode")) is not int:
                raise ValueError("compaction_read_rejected")
            return value.get("data")

        prepared = prepare_compaction_request(
            ref,
            fetch,
            scope=scope,
            model=model,
            question=question,
            context_window_tokens=context_window_tokens,
            output_reserve_tokens=output_reserve_tokens,
            safety_margin_tokens=safety_margin_tokens,
            count_tokens=count_tokens or (lambda text: len(encoder.encode(text, disallowed_special=()))),
            tokenizer_label="cl100k_base_estimate",
        )
    receipt: dict[str, Any] = {"reference": prepared["reference"], "budget": prepared["budget"], "snapshot_only": True, "sent": False}
    if send:
        with OpenAI(api_key=model_key, base_url="https://api.openai.com/v1", max_retries=0, timeout=60) as client:
            response = client.chat.completions.create(**cast(Any, prepared["request"]))
        receipt.update(sent=True, answer=response.choices[0].message.content, provider_usage=response.usage.model_dump() if response.usage else None)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path, help="Four-field JSON copied after explicit artifact inspection")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--question", required=True)
    parser.add_argument("--model", default="gpt-6.1-terra")
    parser.add_argument("--context-window-tokens", type=int, default=16384)
    parser.add_argument("--output-reserve-tokens", type=int, default=512)
    parser.add_argument("--safety-margin-tokens", type=int, default=256)
    parser.add_argument("--send", action="store_true", help="Explicitly send this freshly checked request once; may incur cost")
    args = parser.parse_args()
    try:
        if args.reference.stat().st_size > 2048:
            raise ValueError("invalid_compaction_reference")
        reference = json.loads(args.reference.read_text())
        value = consume(
            reference,
            api_base=os.environ["MULTIRAG_API_BASE"],
            api_token=os.environ["MULTIRAG_API_TOKEN"],
            scope={"user_id": args.user_id, "agent_id": args.agent_id, "session_id": args.session_id},
            model=args.model,
            question=args.question,
            context_window_tokens=args.context_window_tokens,
            output_reserve_tokens=args.output_reserve_tokens,
            safety_margin_tokens=args.safety_margin_tokens,
            send=args.send,
            model_key=os.environ.get("OPENAI_API_KEY"),
        )
    except Exception as error:
        # Provider error messages and raw HTTP bodies may contain sensitive data.
        print("Consumption stopped: " + type(error).__name__, file=sys.stderr)
        return 1
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
