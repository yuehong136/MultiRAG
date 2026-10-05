"""Incremental, credential-free results and labelled screenshot contact sheets."""

import json
import textwrap
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


class AcceptanceError(RuntimeError):
    """A product assertion failed; HTTP success alone is insufficient."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


class Evidence:
    def __init__(self, directory: Path, *, mode: str) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.results: list[dict[str, Any]] = []
        self.screenshots: list[dict[str, Any]] = []
        self.cleanup_record: Path | None = None
        self.completed = False
        self.metadata: dict[str, Any] = {
            "mode": mode,
            "embedding": "controlled 768-dimensional provider; real parser, worker, HTTP, PostgreSQL, Redis, MinIO and Milvus",
            "visual_review": "not_requested" if mode == "api" else "pending_human_review",
            "limits": ["Synthetic text corpus; no PDF/OCR or real-provider quality claim."],
        }
        self.secrets: list[str] = []
        self.save()

    def redact(self, message: str) -> str:
        for secret in self.secrets:
            if secret:
                message = message.replace(secret, "[redacted]")
        return message[:2000]

    def check(self, name: str, operation: Callable[[], Any]) -> bool:
        result: dict[str, Any] = {"name": name, "status": "running"}
        self.results.append(result)
        self.save()
        started = time.monotonic()
        try:
            detail = operation()
            result.update(status="passed", detail=detail)
        except Exception as exc:
            result.update(status="failed", detail=self.redact(f"{type(exc).__name__}: {exc}"))
        finally:
            result["seconds"] = round(time.monotonic() - started, 3)
            self.save()
        print(f"acceptance {result['status'].upper()}: {name}", flush=True)
        return result["status"] == "passed"

    def blocked(self, name: str, reason: str) -> None:
        self.results.append({"name": name, "status": "blocked", "detail": reason, "seconds": 0})
        self.save()

    @property
    def successful(self) -> bool:
        return bool(self.results) and all(result["status"] == "passed" for result in self.results)

    def save(self) -> None:
        status = ("passed" if self.successful else "incomplete_or_failed") if self.completed else "running"
        value = {**self.metadata, "automated_status": status, "checks": self.results, "screenshots": self.screenshots}
        self._write("acceptance.json", json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        lines = ["# Product acceptance", "", f"Automated checks: {value['automated_status']}", f"Visual review: {value['visual_review']}", "", "| Check | Result | Seconds |", "| --- | --- | --- |"]
        for result in self.results:
            lines.append(f"| {result['name']} | {result['status']} | {result.get('seconds', '')} |")
        for result in self.results:
            if result["status"] in {"failed", "blocked"}:
                lines.extend(["", f"**{result['name']}**: {result.get('detail')}"])
        lines.extend(["", "Embedding: " + self.metadata["embedding"], "", *self.metadata["limits"], "", "Visual evidence requires human review; screenshots do not approve a design."])
        for sheet in sorted(self.directory.glob("contact-sheet-*.jpg")):
            lines.extend(["", f"![{sheet.name}]({sheet.name})"])
        self._write("acceptance.md", "\n".join(lines) + "\n")

    def finish(self) -> None:
        self.completed = True
        self.save()

    def _write(self, name: str, content: str) -> None:
        path = self.directory / name
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.touch(mode=0o600)
        temporary.write_text(content)
        temporary.replace(path)

    def contact_sheets(self) -> list[str]:
        """Six labelled views per sheet; full screenshots remain available."""
        sheets = []
        font = ImageFont.load_default(size=16)
        for offset in range(0, len(self.screenshots), 6):
            views = self.screenshots[offset : offset + 6]
            sheet = Image.new("RGB", (1440, 640 * ((len(views) + 1) // 2)), "#e5e7eb")
            draw = ImageDraw.Draw(sheet)
            for index, view in enumerate(views):
                x, y = index % 2 * 720, index // 2 * 640
                route = "\n".join(textwrap.wrap(view["route"], width=70))
                label = f"{view['file']}\n{route}\n{view['viewport']} | {view['theme']} | {view['status']}"
                draw.multiline_text((x + 12, y + 8), label, font=font, fill="#111827", spacing=4)
                with Image.open(self.directory / view["file"]) as screenshot:
                    screenshot.thumbnail((700, 475))
                    sheet.paste(screenshot.convert("RGB"), (x + 10, y + 155))
            filename = f"contact-sheet-{offset // 6 + 1:02}.jpg"
            sheet.save(self.directory / filename, quality=90)
            sheets.append(filename)
        self.save()
        return sheets
