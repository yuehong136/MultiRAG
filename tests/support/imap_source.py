"""Controlled imaplib wire results, including stable UID and INTERNALDATE data."""

from __future__ import annotations

from email.message import EmailMessage
from typing import Any


def message(identifier: str | None = "<retained@test>", *, date: str | None = "Wed, 01 Jan 2025 12:00:00 +0000", attachment: bool = True) -> bytes:
    result = EmailMessage()
    if identifier is not None:
        result["Message-ID"] = identifier
    if date is not None:
        result["Date"] = date
    result["Subject"] = "Mail subject"
    result["From"] = "Sender <sender@test>"
    result["To"] = "Recipient <recipient@test>"
    result.set_content("Mail body")
    if attachment:
        result.add_attachment(b"attachment body", maintype="application", subtype="octet-stream", filename="报告.txt")
        result.set_boundary("fixed-test-boundary")
    return result.as_bytes()


class ImapSource:
    def __init__(self, mode: str = "complete", messages: dict[str, bytes] | None = None) -> None:
        self.mode = mode
        self.messages = messages if messages is not None else {"1": message()}
        self.mailbox = ""
        self.calls: list[tuple[Any, ...]] = []
        self.logged_out = 0

    def list(self, *args: Any) -> tuple[str, list[Any]]:
        self.calls.append(("LIST",))
        if self.mode == "list-error":
            return "NO", [b"denied"]
        if self.mode == "malformed":
            return "OK", [b"broken listing"]
        if self.mode == "empty-mailboxes":
            return "OK", [None]
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) NIL "Archive"', b'(\\Noselect) "/" "Parent"']

    def select(self, mailbox: str, readonly: bool) -> tuple[str, list[bytes]]:
        self.mailbox = mailbox
        self.calls.append(("SELECT", mailbox, readonly))
        if self.mode == "select-error":
            return "NO", [b"denied"]
        return "OK", [b"1"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.calls.append((command, self.mailbox, *args))
        if command == "SEARCH":
            assert args == (None, "ALL")
            if self.mode == "search-error" or (self.mode == "second-mailbox-error" and self.mailbox == '"Archive"'):
                return "NO", [b""]
            if self.mode == "bad-search":
                return "OK", [None]
            ids = b"" if self.mode == "empty" or self.mailbox == '"Archive"' else " ".join(self.messages).encode()
            return "OK", [ids]
        assert command == "FETCH" and args[1] == "(BODY.PEEK[] INTERNALDATE)"
        uid = args[0]
        if self.mode == "read-error" or (self.mode == "later-read-error" and uid != "1"):
            return "NO", []
        if self.mode == "missing-read":
            return "OK", [None]
        metadata = f'1 (UID {uid if self.mode != "wrong-uid" else "999"} INTERNALDATE "07-Oct-2026 12:00:00 +0800" BODY[] {{1}}'.encode()
        return "OK", [(metadata, self.messages[uid]), b")"]

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out += 1
        return "BYE", [b"logout"]
