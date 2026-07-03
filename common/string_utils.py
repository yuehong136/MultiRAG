import re


def remove_redundant_spaces(txt: str):
    """
    Remove redundant spaces around punctuation marks while preserving meaningful spaces.

    This function performs two main operations:
    1. Remove spaces after left-boundary characters (opening brackets, etc.)
    2. Remove spaces before right-boundary characters (closing brackets, punctuation, etc.)

    Args:
        txt (str): Input text to process

    Returns:
        str: Text with redundant spaces removed
    """
    # First pass: Remove spaces after left-boundary characters
    # Matches: [non-alphanumeric-and-specific-right-punctuation] + [non-space]
    # Removes spaces after characters like '(', '<', and other non-alphanumeric chars
    # Examples:
    #   "( test" → "(test"
    txt = re.sub(r"([^a-z0-9.,\)>]) +([^ ])", r"\1\2", txt, flags=re.IGNORECASE)

    # Second pass: Remove spaces before right-boundary characters
    # Matches: [non-space] + [non-alphanumeric-and-specific-left-punctuation]
    # Removes spaces before characters like non-')', non-',', non-'.', and non-alphanumeric chars
    # Examples:
    #   "world !" → "world!"
    return re.sub(r"([^ ]) +([^a-z0-9.,\(<])", r"\1\2", txt, flags=re.IGNORECASE)


def clean_markdown_block(text):
    """
    Remove Markdown code block syntax from the beginning and end of text.

    This function cleans Markdown code blocks by removing:
    - Opening ```Markdown tags (with optional whitespace and newlines)
    - Closing ``` tags (with optional whitespace and newlines)

    Args:
        text (str): Input text that may be wrapped in Markdown code blocks

    Returns:
        str: Cleaned text with Markdown code block syntax removed, and stripped of surrounding whitespace

    """
    # Remove opening ```Markdown tag with optional whitespace and newlines
    # Matches: optional whitespace + ```markdown + optional whitespace + optional newline
    text = re.sub(r"^\s*```markdown\s*\n?", "", text)

    # Remove closing ``` tag with optional whitespace and newlines
    # Matches: optional newline + optional whitespace + ``` + optional whitespace at end
    text = re.sub(r"\n?\s*```\s*$", "", text)

    # Return text with surrounding whitespace removed
    return text.strip()


def truncate_utf8_bytes(text: str, max_bytes: int) -> str:
    """
    Truncate a string to at most `max_bytes` in UTF-8 byte length.
    """
    if max_bytes <= 0:
        return ""
    if not text:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text

    truncated = raw[:max_bytes]
    while truncated:
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    return ""


def split_and_sanitize_terms(
    raw: str | list[str] | tuple[str, ...] | None,
    split_pattern: str = r"[,，;；、\n\r|]+",
    max_term_bytes: int | None = None,
    max_terms: int | None = None,
) -> list[str]:
    """
    Split text/list into clean terms with optional UTF-8 byte-length limit.
    """
    if raw is None:
        return []

    if isinstance(raw, str):
        chunks = [raw]
    elif isinstance(raw, (list, tuple)):
        chunks = [str(item) for item in raw if item is not None]
    else:
        chunks = [str(raw)]

    terms: list[str] = []
    seen: set[str] = set()

    for chunk in chunks:
        for term in re.split(split_pattern, chunk):
            cleaned = term.strip().strip('"').strip("'")
            if not cleaned:
                continue
            if max_term_bytes is not None:
                cleaned = truncate_utf8_bytes(cleaned, max_term_bytes).strip()
                if not cleaned:
                    continue
            if cleaned in seen:
                continue
            seen.add(cleaned)
            terms.append(cleaned)
            if max_terms is not None and len(terms) >= max_terms:
                return terms
    return terms
