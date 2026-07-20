"""
Canvas-free Parser facade.

The analyze_v2 direct-file path still needs a simple ``parse_file`` helper, but
the parsing behavior must stay aligned with the runtime Parser component. This
module therefore only normalizes caller configs, creates a lightweight Parser
process, and dispatches to the Parser runtime methods.
"""

import logging
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from common.misc_utils import thread_pool_exec
from core.flow.parser.parser import Parser, ParserParam

logger = logging.getLogger(__name__)


_SETUP_CONFIG_KEYS = {
    "pdf": ("pdf", "pdf_config"),
    "spreadsheet": ("spreadsheet", "excel", "spreadsheet_config", "excel_config"),
    "doc": ("doc", "doc_config", "word", "word_config"),
    "docx": ("docx", "docx_config", "word", "word_config"),
    "markdown": ("markdown", "text&markdown", "text", "markdown_config"),
    "text&code": ("text&code", "code", "code_config"),
    "html": ("html", "html_config"),
    "slides": ("slides", "ppt", "ppt_config", "slides_config"),
    "image": ("image", "image_config"),
    "email": ("email", "email_config"),
    "audio": ("audio", "audio_config"),
    "video": ("video", "video_config"),
    "epub": ("epub", "epub_config"),
}

_METHOD_BY_SETUP = {
    "pdf": "_pdf",
    "spreadsheet": "_spreadsheet",
    "doc": "_doc",
    "docx": "_docx",
    "markdown": "_markdown",
    "text&code": "_code",
    "html": "_html",
    "slides": "_slides",
    "image": "_image",
    "email": "_email",
    "audio": "_audio",
    "video": "_video",
    "epub": "_epub",
}


def _merge_dicts(*configs: dict | None) -> dict:
    merged = {}
    for config in configs:
        if config:
            merged.update({key: value for key, value in config.items() if value is not None})
    return merged


def _normalize_bool_string(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _normalize_image_config(config: dict | None) -> dict:
    normalized = dict(config or {})
    parse_method = str(normalized.get("parse_method") or "ocr")
    lowered = parse_method.lower()

    # analyze_v2 historically routed generic parser modes to OCR for raw images.
    if lowered in {"auto", "deepdoc", "plain_text"}:
        normalized["parse_method"] = "ocr"
    elif lowered == "vlm":
        normalized["parse_method"] = normalized.get("llm_name") or normalized.get("llm_id") or parse_method
    else:
        normalized["parse_method"] = parse_method

    if normalized.get("llm_name") and not normalized.get("llm_id"):
        normalized["llm_id"] = normalized["llm_name"]
    normalized.pop("llm_name", None)
    return normalized


def _normalize_setup_config(setup_key: str, config: dict | None) -> dict:
    normalized = dict(config or {})
    if setup_key in {"markdown", "html"} and "remove_toc" in normalized:
        normalized["remove_toc"] = _normalize_bool_string(normalized["remove_toc"])
    if setup_key == "image":
        normalized = _normalize_image_config(normalized)
    if setup_key in {"audio", "video"} and "vlm" not in normalized and "llm_id" in normalized:
        normalized["vlm"] = {"llm_id": normalized.pop("llm_id")}
    return normalized


def _config_from_parser_config(parser_config: dict | None, setup_key: str) -> dict:
    if not parser_config:
        return {}
    for key in _SETUP_CONFIG_KEYS[setup_key]:
        config = parser_config.get(key)
        if config:
            return dict(config)
    return {}


def _build_parser_param(
    *,
    parser_config: dict | None = None,
    pdf_config: dict | None = None,
    excel_config: dict | None = None,
    word_config: dict | None = None,
    image_config: dict | None = None,
    email_config: dict | None = None,
    slides_config: dict | None = None,
    markdown_config: dict | None = None,
    video_config: dict | None = None,
    audio_config: dict | None = None,
    epub_config: dict | None = None,
    code_config: dict | None = None,
    html_config: dict | None = None,
) -> ParserParam:
    param = ParserParam()
    direct_configs = {
        "pdf": pdf_config,
        "spreadsheet": excel_config,
        "doc": word_config,
        "docx": word_config,
        "markdown": markdown_config,
        "text&code": code_config,
        "html": html_config,
        "slides": slides_config,
        "image": image_config,
        "email": email_config,
        "audio": audio_config,
        "video": video_config,
        "epub": epub_config,
    }

    for setup_key, direct_config in direct_configs.items():
        merged = _merge_dicts(_config_from_parser_config(parser_config, setup_key), direct_config)
        if merged:
            param.setups[setup_key].update(_normalize_setup_config(setup_key, merged))

    return param


def _make_process(param: ParserParam, tenant_id: str, callback=None) -> Parser:
    process = Parser.__new__(Parser)
    process._param = param
    process._id = "parser-utils"

    def get_tenant_id():
        return tenant_id

    process._canvas = SimpleNamespace(
        _tenant_id=tenant_id,
        _doc_id=None,
        get_tenant_id=get_tenant_id,
    )

    def _callback(prog, msg=""):
        if callback:
            callback(prog, msg)

    process.callback = _callback
    return process


def _select_setup(param: ParserParam, filename: str) -> str:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    for setup_key, config in param.setups.items():
        if ext in config.get("suffix", []):
            return setup_key
    raise ValueError(f"Unsupported file extension: {ext}")


def _check_selected_setup(param: ParserParam, setup_key: str) -> None:
    if setup_key in {"audio", "video"}:
        return

    config = param.setups.get(setup_key) or {}
    output_format = config.get("output_format")
    allowed = param.allowed_output_format.get(setup_key, [])
    if allowed and output_format:
        param.check_valid_value(output_format, f"{setup_key} output format abnormal.", allowed)


async def _invoke_parser_method(process: Parser, setup_key: str, filename: str, binary: bytes) -> dict:
    method = getattr(process, _METHOD_BY_SETUP[setup_key])
    file_meta = {
        "name": filename,
        "id": "__direct_bytes__",
        "created_by": process._canvas.get_tenant_id(),
    }

    await thread_pool_exec(method, filename, binary, file=file_meta)
    error = process.output("_ERROR")
    if error:
        raise ValueError(error)
    return process.output()


async def parse_file(
    filename: str,
    binary: bytes,
    tenant_id: str,
    pdf_config: dict | None = None,
    excel_config: dict | None = None,
    word_config: dict | None = None,
    image_config: dict | None = None,
    email_config: dict | None = None,
    slides_config: dict | None = None,
    markdown_config: dict | None = None,
    video_config: dict | None = None,
    audio_config: dict | None = None,
    epub_config: dict | None = None,
    callback=None,
    *,
    code_config: dict | None = None,
    html_config: dict | None = None,
    parser_config: dict | None = None,
) -> dict:
    """Parse direct bytes with the same runtime code used by the Parser node."""
    param = _build_parser_param(
        parser_config=parser_config,
        pdf_config=pdf_config,
        excel_config=excel_config,
        word_config=word_config,
        image_config=image_config,
        email_config=email_config,
        slides_config=slides_config,
        markdown_config=markdown_config,
        video_config=video_config,
        audio_config=audio_config,
        epub_config=epub_config,
        code_config=code_config,
        html_config=html_config,
    )
    setup_key = _select_setup(param, filename)
    _check_selected_setup(param, setup_key)
    process = _make_process(param, tenant_id, callback)
    logger.debug("FlowParser dispatch: filename=%s setup=%s", filename, setup_key)
    return await _invoke_parser_method(process, setup_key, filename, binary)


class FlowParser:
    """Backward-compatible facade over :func:`parse_file`."""

    @staticmethod
    async def parse_file(*args, **kwargs) -> dict:
        return await parse_file(*args, **kwargs)

    @staticmethod
    async def parse_pdf(
        filename: str,
        binary: bytes,
        tenant_id: str,
        parse_method: str = "deepdoc",
        output_format: str = "json",
        lang: str = "Chinese",
        callback=None,
        table_context_size: int = 0,
        image_context_size: int = 0,
        preprocess: list | None = None,
        **method_kwargs,
    ) -> dict:
        pdf_config = {
            "parse_method": parse_method,
            "output_format": output_format,
            "lang": lang,
            "table_context_size": table_context_size,
            "image_context_size": image_context_size,
            "preprocess": preprocess or [],
            **method_kwargs,
        }
        return await parse_file(filename, binary, tenant_id, pdf_config=pdf_config, callback=callback)

    @staticmethod
    async def parse_excel(
        filename: str,
        binary: bytes,
        output_format: str = "html",
        callback=None,
        parse_method: str = "deepdoc",
        table_context_size: int = 0,
        image_context_size: int = 0,
        **method_kwargs,
    ) -> dict:
        excel_config = {
            "parse_method": parse_method,
            "output_format": output_format,
            "table_context_size": table_context_size,
            "image_context_size": image_context_size,
            **method_kwargs,
        }
        return await parse_file(filename, binary, "", excel_config=excel_config, callback=callback)

    @staticmethod
    async def parse_word(
        filename: str,
        binary: bytes,
        output_format: str = "json",
        callback=None,
        table_context_size: int = 0,
        image_context_size: int = 0,
        preprocess: list | None = None,
        **method_kwargs,
    ) -> dict:
        word_config = {
            "output_format": output_format,
            "table_context_size": table_context_size,
            "image_context_size": image_context_size,
            "preprocess": preprocess or [],
            **method_kwargs,
        }
        return await parse_file(filename, binary, "", word_config=word_config, callback=callback)

    @staticmethod
    async def parse_ppt(
        filename: str,
        binary: bytes,
        callback=None,
        output_format: str = "json",
        parse_method: str = "deepdoc",
        table_context_size: int = 0,
        image_context_size: int = 0,
        **method_kwargs,
    ) -> dict:
        slides_config = {
            "parse_method": parse_method,
            "output_format": output_format,
            "table_context_size": table_context_size,
            "image_context_size": image_context_size,
            **method_kwargs,
        }
        return await parse_file(filename, binary, "", slides_config=slides_config, callback=callback)

    @staticmethod
    async def parse_markdown(
        filename: str,
        binary: bytes,
        output_format: str = "json",
        callback=None,
        table_context_size: int = 0,
        image_context_size: int = 0,
        delimiter: str | None = None,
        preprocess: list | None = None,
        **method_kwargs,
    ) -> dict:
        markdown_config = {
            "output_format": output_format,
            "table_context_size": table_context_size,
            "image_context_size": image_context_size,
            "delimiter": delimiter,
            "preprocess": preprocess or [],
            **method_kwargs,
        }
        return await parse_file(filename, binary, "", markdown_config=markdown_config, callback=callback)

    @staticmethod
    async def parse_code(
        filename: str,
        binary: bytes,
        output_format: str = "text",
        callback=None,
        **method_kwargs,
    ) -> dict:
        return await parse_file(
            filename,
            binary,
            "",
            code_config={"output_format": output_format, **method_kwargs},
            callback=callback,
        )

    @staticmethod
    async def parse_html(
        filename: str,
        binary: bytes,
        output_format: str = "text",
        callback=None,
        **method_kwargs,
    ) -> dict:
        return await parse_file(
            filename,
            binary,
            "",
            html_config={"output_format": output_format, **method_kwargs},
            callback=callback,
        )

    @staticmethod
    async def parse_image(
        filename: str,
        binary: bytes,
        tenant_id: str,
        parse_method: str = "ocr",
        llm_name: str | None = None,
        lang: str = "Chinese",
        system_prompt: str | None = None,
        callback=None,
    ) -> dict:
        image_config = {
            "parse_method": parse_method,
            "llm_name": llm_name,
            "lang": lang,
            "system_prompt": system_prompt,
        }
        return await parse_file(filename, binary, tenant_id, image_config=image_config, callback=callback)

    @staticmethod
    async def parse_audio(
        filename: str,
        binary: bytes,
        tenant_id: str,
        callback=None,
        llm_id: str = "",
    ) -> dict:
        return await parse_file(filename, binary, tenant_id, audio_config={"vlm": {"llm_id": llm_id}}, callback=callback)

    @staticmethod
    async def parse_video(
        filename: str,
        binary: bytes,
        tenant_id: str,
        llm_name: str | None = None,
        prompt: str = "",
        callback=None,
    ) -> dict:
        return await parse_file(
            filename,
            binary,
            tenant_id,
            video_config={"vlm": {"llm_id": llm_name}, "prompt": prompt},
            callback=callback,
        )

    @staticmethod
    async def parse_email(
        filename: str,
        binary: bytes,
        output_format: str = "json",
        fields: list[str] | None = None,
        callback=None,
    ) -> dict:
        return await parse_file(
            filename,
            binary,
            "",
            email_config={"output_format": output_format, "fields": fields},
            callback=callback,
        )

    @staticmethod
    async def parse_epub(
        filename: str,
        binary: bytes,
        output_format: str = "json",
        callback=None,
        chunk_token_num: int = 512,
    ) -> dict:
        return await parse_file(
            filename,
            binary,
            "",
            epub_config={"output_format": output_format, "chunk_token_num": chunk_token_num},
            callback=callback,
        )


def clone_parser_setups(**configs: dict | None) -> dict[str, dict[str, Any]]:
    """Return effective Parser setups for tests and diagnostics."""
    param = _build_parser_param(**configs)
    return deepcopy(param.setups)
