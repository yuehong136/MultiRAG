from __future__ import annotations

import re
import unicodedata


ARABIC_PRESENTATION_FORMS_RE = re.compile(r"[\uFB50-\uFDFF\uFE70-\uFEFF]")


def normalize_arabic_digits(text: str | None) -> str | None:
    if text is None or not isinstance(text, str):
        return text

    out = []
    for ch in text:
        code = ord(ch)
        if 0x0660 <= code <= 0x0669:
            out.append(chr(code - 0x0660 + 0x30))
        elif 0x06F0 <= code <= 0x06F9:
            out.append(chr(code - 0x06F0 + 0x30))
        else:
            out.append(ch)
    return "".join(out)


def normalize_arabic_presentation_forms(text: str | None) -> str | None:
    """Normalize Arabic presentation forms to canonical text when present."""
    if text is None or not isinstance(text, str):
        return text
    if not ARABIC_PRESENTATION_FORMS_RE.search(text):
        return text
    return unicodedata.normalize("NFKC", text)