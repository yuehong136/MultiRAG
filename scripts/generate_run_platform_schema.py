"""Generate or verify the canonical RUN-F1a JSON Schemas."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from api.run_platform.schemas import (
    MessageDeltaEvent,
    RunEventEnvelope,
)

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "api" / "run_platform" / "protocol" / "generated"
SCHEMA_TYPES: dict[str, Any] = {
    "message-delta-event.schema.json": MessageDeltaEvent,
    "run-event-envelope.schema.json": RunEventEnvelope,
}


def render_schema(model_type: Any) -> str:
    schema = TypeAdapter(model_type).json_schema(mode="validation")
    schema["$schema"] = SCHEMA_DIALECT
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def generate_schemas(output_dir: Path, *, check: bool) -> list[str]:
    drift: list[str] = []
    if not check:
        output_dir.mkdir(parents=True, exist_ok=True)

    expected_files = set(SCHEMA_TYPES)
    for filename, model_type in SCHEMA_TYPES.items():
        destination = output_dir / filename
        expected = render_schema(model_type)
        if check:
            if not destination.exists() or destination.read_text(encoding="utf-8") != expected:
                drift.append(filename)
            continue
        destination.write_text(expected, encoding="utf-8")

    stale_files = {path.name for path in output_dir.glob("*.schema.json")} - expected_files
    if check:
        drift.extend(sorted(stale_files))
    else:
        for filename in stale_files:
            (output_dir / filename).unlink()
    return sorted(drift)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    drift = generate_schemas(args.output_dir, check=args.check)
    if drift:
        parser.error("Run Platform schema drift: " + ", ".join(drift) + ". Regenerate before committing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
