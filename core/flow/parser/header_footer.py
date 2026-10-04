"""Structural header/footer filtering for pipeline document parsers."""

from io import BytesIO
from typing import Any

from bs4 import BeautifulSoup
from docx import Document

from core.nlp import find_codec


def parser_flag(value: Any) -> bool:
    """Accept legacy serialized booleans without treating 'false' as truthy."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError(f"Expected a boolean parser flag, got {type(value).__name__}")


def is_header_footer_layout(value: Any) -> bool:
    layout = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    return " ".join(layout.split()) in {"header", "footer", "number", "page header", "page footer", "page number"}


def remove_header_footer_html_blob(blob: bytes | str, *, tika: bool = False) -> bytes:
    text = blob.decode(find_codec(blob), errors="replace") if isinstance(blob, bytes) else blob
    soup = BeautifulSoup(text, "html.parser")
    selectors = "header, footer, [role~='banner'], [role~='contentinfo']"
    if tika:
        selectors += ", .header, .footer"
    # Remove only structural containers, never matching text elsewhere in the body.
    for element in soup.select(selectors):
        element.decompose()
    return str(soup).encode("utf-8")


def remove_header_footer_docx_blob(blob: bytes) -> bytes:
    document = Document(BytesIO(blob))
    for section in document.sections:
        for container in (
            section.header,
            section.footer,
            section.first_page_header,
            section.first_page_footer,
            section.even_page_header,
            section.even_page_footer,
        ):
            if container.is_linked_to_previous:
                continue
            for child in list(container._element):
                container._element.remove(child)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def word_content_lines(content: str, *, remove_header_footer: bool) -> list[str]:
    if remove_header_footer:
        filtered = remove_header_footer_html_blob(content, tika=True)
        content = BeautifulSoup(filtered, "html.parser").get_text("\n")
    return [line.strip() for line in content.splitlines() if line.strip()]
