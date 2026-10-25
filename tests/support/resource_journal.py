"""Case identity and reserve-before-create events linked to the outer runner."""

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts.resource_journal import Journal


class ResourceJournal:
    """Keep an independent append-only case history; fail closed when gated."""

    def __init__(self, case: str) -> None:
        self.run_id = uuid4().hex
        root = Path(os.environ.get("REST_DOCUMENT_RESOURCE_DIR", "/tmp/multirag-rest-document-resources"))
        self.path = root / f"{self.run_id}.jsonl"
        parent = os.environ.get("MULTIRAG_RUN_JOURNAL")
        self.parent_run_id = os.environ.get("MULTIRAG_RUN_ID")
        if self.parent_run_id:
            if not parent:
                raise ValueError("registered run requires an existing parent journal")
            journal = Journal(Path(parent))
            try:
                journal.event("reserve-case-journal", parent_run_id=self.parent_run_id, case_id=self.run_id, path=str(self.path), case=case)
            finally:
                journal.close()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        journal = Journal(self.path, create=True)
        try:
            journal.event("case", case_id=self.run_id, parent_run_id=self.parent_run_id, nodeid=case, pid=os.getpid())
        finally:
            journal.close()

    def event(self, event: str, **fields: Any) -> None:
        journal = Journal(self.path)
        try:
            journal.event(event, **{"run_id": self.run_id, "parent_run_id": self.parent_run_id, "case_id": self.run_id, **fields})
        finally:
            journal.close()
