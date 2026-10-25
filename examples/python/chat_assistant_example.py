"""List chats; opt in to an owned assistant/session with two completion modes."""

import os
from uuid import uuid4

from common import request, show


def main() -> None:
    if os.environ.get("MULTIRAG_ALLOW_WRITE") != "1":
        show(request("GET", "/chats?page=1&page_size=10"))
        return
    chat = request("POST", "/chats", {"name": "example-" + uuid4().hex, "dataset_ids": [], "prompt_config": {"system": "Answer briefly", "parameters": []}})
    chat_id = chat["id"]
    # Cleanup is scoped to IDs returned by this invocation, including on errors.
    try:
        session = request("POST", f"/chats/{chat_id}/sessions", {"name": "Example session"})
        try:
            for stream in (False, True):
                payload = {"chat_id": chat_id, "session_id": session["id"], "messages": [{"role": "user", "content": "What is retrieval augmented generation?"}], "stream": stream}
                show(request("POST", "/chat/completions", payload, stream=stream))
            show(request("GET", f"/chats/{chat_id}/sessions"))
        finally:
            request("DELETE", f"/chats/{chat_id}/sessions", {"ids": [session["id"]]})
    finally:
        request("DELETE", f"/chats/{chat_id}")


if __name__ == "__main__":
    main()
