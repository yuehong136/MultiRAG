"""List chunks; opt in to add, PATCH and delete one uniquely named chunk."""

import os
from uuid import uuid4

from common import request, required, show


def main() -> None:
    path = f"/datasets/{required('MULTIRAG_DATASET_ID')}/documents/{required('MULTIRAG_DOCUMENT_ID')}/chunks"
    show(request("GET", path + "?page=1&page_size=10"))
    if os.environ.get("MULTIRAG_ALLOW_WRITE") != "1":
        return
    # Unique content avoids deleting an existing deterministic-ID manual chunk.
    marker = uuid4().hex
    chunk = request("POST", path, {"content": "Example chunk " + marker})["chunk"]
    try:
        request("PATCH", path + "/" + chunk["id"], {"content": "Updated example chunk " + marker})
        show(request("GET", path + "/" + chunk["id"]))
    finally:
        request("DELETE", path, {"chunk_ids": [chunk["id"]]})


if __name__ == "__main__":
    main()
