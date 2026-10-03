#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import base64
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from io import BytesIO
from os import PathLike
from pathlib import Path
from typing import Any, ClassVar, Literal, get_type_hints
from urllib.parse import quote, urlsplit, urlunsplit

import numpy as np
import pdfplumber
import requests
from PIL import Image
from pydantic import TypeAdapter, ValidationError

from common.constants import MAXIMUM_PAGE_NUMBER

try:
    from deepdoc.parser.pdf_parser import RAGFlowPdfParser
except Exception:

    class RAGFlowPdfParser:
        pass


from deepdoc.parser.utils import extract_pdf_outlines

AlgorithmType = Literal["PaddleOCR-VL", "PP-OCRv5", "PP-StructureV3", "PaddleOCR-VL-1.5"]
SUPPORTED_PADDLEOCR_ALGORITHMS: tuple[AlgorithmType, ...] = ("PaddleOCR-VL", "PP-OCRv5", "PP-StructureV3", "PaddleOCR-VL-1.5")
SectionTuple = tuple[str, ...]
TableTuple = tuple[str, ...]
ParseResult = tuple[list[SectionTuple], list[TableTuple]]


_MARKDOWN_IMAGE_PATTERN = re.compile(
    r"""
        <div[^>]*>\s*
        <img[^>]*/>\s*
        </div>
        |
        <img[^>]*/>
        """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)


def _remove_images_from_markdown(markdown: str) -> str:
    return _MARKDOWN_IMAGE_PATTERN.sub("", markdown)


def _normalize_bbox(bbox: list[Any] | tuple[Any, ...]) -> tuple[float, float, float, float]:
    if len(bbox) < 4:
        return 0.0, 0.0, 0.0, 0.0

    left, top, right, bottom = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    if left > right:
        left, right = right, left
    if top > bottom:
        top, bottom = bottom, top
    return left, top, right, bottom


@dataclass
class PaddleOCRVLConfig:
    """Configuration for PaddleOCR-VL algorithm."""

    use_doc_orientation_classify: bool | None = False
    use_doc_unwarping: bool | None = False
    use_layout_detection: bool | None = None
    use_chart_recognition: bool | None = None
    use_seal_recognition: bool | None = None
    use_ocr_for_image_block: bool | None = None
    layout_threshold: float | dict | None = None
    layout_nms: bool | None = None
    layout_unclip_ratio: float | tuple[float, float] | list[float] | dict | None = None
    layout_merge_bboxes_mode: str | dict | None = None
    layout_shape_mode: str | None = None
    prompt_label: str | None = None
    format_block_content: bool | None = True
    repetition_penalty: float | None = None
    temperature: float | None = None
    top_p: float | None = None
    min_pixels: int | None = None
    max_pixels: int | None = None
    max_new_tokens: int | None = None
    merge_layout_blocks: bool | None = False
    markdown_ignore_labels: list[str] | None = None
    vlm_extra_args: dict | None = None
    restructure_pages: bool | None = False
    merge_tables: bool | None = None
    relevel_titles: bool | None = None


@dataclass
class PaddleOCRTextConfig:
    """Parameters accepted by the general OCR /ocr service."""

    use_doc_orientation_classify: bool | None = False
    use_doc_unwarping: bool | None = False
    use_textline_orientation: bool | None = None
    text_det_limit_side_len: int | None = None
    text_det_limit_type: str | None = None
    text_det_thresh: float | None = None
    text_det_box_thresh: float | None = None
    text_det_unclip_ratio: float | None = None
    text_rec_score_thresh: float | None = None


@dataclass
class PaddleOCRStructureConfig(PaddleOCRTextConfig):
    """Parameters accepted by the PP-StructureV3 layout service."""

    use_seal_recognition: bool | None = None
    use_table_recognition: bool | None = None
    use_formula_recognition: bool | None = None
    use_chart_recognition: bool | None = None
    use_region_detection: bool | None = None
    format_block_content: bool | None = True
    layout_threshold: float | dict | None = None
    layout_nms: bool | None = None
    layout_unclip_ratio: float | tuple[float, float] | list[float] | dict | None = None
    layout_merge_bboxes_mode: str | dict | None = None
    seal_det_limit_side_len: int | None = None
    seal_det_limit_type: str | None = None
    seal_det_thresh: float | None = None
    seal_det_box_thresh: float | None = None
    seal_det_unclip_ratio: float | None = None
    seal_rec_score_thresh: float | None = None
    use_wired_table_cells_trans_to_html: bool | None = None
    use_wireless_table_cells_trans_to_html: bool | None = None
    use_table_orientation_classify: bool | None = None
    use_ocr_results_with_table_cells: bool | None = None
    use_e2e_wired_table_rec_model: bool | None = None
    use_e2e_wireless_table_rec_model: bool | None = None
    markdown_ignore_labels: list[str] | None = None


def _algorithm_config_type(algorithm: str) -> type[PaddleOCRVLConfig] | type[PaddleOCRTextConfig]:
    if algorithm not in SUPPORTED_PADDLEOCR_ALGORITHMS:
        raise ValueError(f"Unsupported algorithm: {algorithm}")
    if algorithm == "PP-OCRv5":
        return PaddleOCRTextConfig
    if algorithm == "PP-StructureV3":
        return PaddleOCRStructureConfig
    return PaddleOCRVLConfig


def _algorithm_defaults(algorithm: str) -> dict[str, Any]:
    return asdict(_algorithm_config_type(algorithm)())


def _api_field_name(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(part.capitalize() for part in rest)


def _uses_job_api(api_url: str) -> bool:
    return urlsplit(api_url).path.rstrip("/").endswith("/api/v2/ocr/jobs")


def _validated_inference_result(response_data: Any) -> dict[str, Any]:
    if not isinstance(response_data, dict) or type(response_data.get("errorCode")) is not int or response_data.get("errorCode") != 0 or not isinstance(response_data.get("result"), dict):
        raise RuntimeError("[PaddleOCR] invalid response format")
    return response_data["result"]


@dataclass
class PaddleOCRConfig:
    """Main configuration for PaddleOCR parser."""

    api_url: str = ""
    access_token: str | None = None
    algorithm: AlgorithmType = "PaddleOCR-VL"
    request_timeout: int = 600
    prettify_markdown: bool = True
    show_formula_number: bool = True
    visualize: bool = False
    additional_params: dict[str, Any] = field(default_factory=dict)
    algorithm_config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        defaults = _algorithm_defaults(self.algorithm)
        if not isinstance(self.request_timeout, int) or isinstance(self.request_timeout, bool) or self.request_timeout <= 0:
            raise ValueError("PaddleOCR request_timeout must be a positive integer")
        if not isinstance(self.algorithm_config, dict) or not isinstance(self.additional_params, dict):
            raise ValueError("PaddleOCR algorithm_config and additional_params must be objects")
        unknown = self.algorithm_config.keys() - defaults.keys()
        if unknown:
            raise ValueError(f"Unsupported {self.algorithm} configuration fields: {', '.join(sorted(unknown))}")
        hints = get_type_hints(_algorithm_config_type(self.algorithm))
        for name, value in self.algorithm_config.items():
            try:
                TypeAdapter(hints[name]).validate_python(value, strict=True)
            except ValidationError as exc:
                raise ValueError(f"PaddleOCR {name} has an invalid type") from exc
        if not isinstance(self.api_url, str) or (self.access_token is not None and not isinstance(self.access_token, str)):
            raise ValueError("PaddleOCR api_url and access_token must be strings")
        if self.api_url:
            parsed_url = urlsplit(self.api_url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
                raise ValueError("PaddleOCR api_url must be an HTTP or HTTPS URL")
            if _uses_job_api(self.api_url) and not (self.access_token and self.access_token.strip()):
                raise ValueError("PaddleOCR Job API requires an access token")
        if any(not isinstance(value, bool) for value in (self.prettify_markdown, self.show_formula_number, self.visualize)):
            raise ValueError("PaddleOCR Markdown and visualize settings must be booleans")
        self.algorithm_config = defaults | {name: value for name, value in self.algorithm_config.items() if value is not None}

    @classmethod
    def from_dict(cls, config: dict[str, Any] | None) -> "PaddleOCRConfig":
        """Create configuration from dictionary."""
        cfg = (config or {}).copy()
        algorithm = cfg.get("algorithm", "PaddleOCR-VL")

        # Validate algorithm
        _algorithm_defaults(algorithm)

        # Extract algorithm-specific configuration
        algorithm_config: dict[str, Any] = {}
        algorithm_config_user = cfg.get("algorithm_config")
        if algorithm_config_user is not None:
            if not isinstance(algorithm_config_user, dict):
                raise ValueError("PaddleOCR algorithm_config must be an object")
            algorithm_config.update(algorithm_config_user)

        # Remove processed keys
        cfg.pop("algorithm_config", None)

        # Prepare initialization arguments
        field_names = {field.name for field in fields(cls)}
        init_kwargs: dict[str, Any] = {}

        for field_name in field_names:
            if field_name in cfg:
                init_kwargs[field_name] = cfg[field_name]

        init_kwargs["algorithm_config"] = algorithm_config

        return cls(**init_kwargs)

    @classmethod
    def from_kwargs(cls, **kwargs: Any) -> "PaddleOCRConfig":
        """Create configuration from keyword arguments."""
        return cls.from_dict(kwargs)


class PaddleOCRParser(RAGFlowPdfParser):
    """Parser for PDF documents using PaddleOCR API."""

    _ZOOMIN = 2

    _COMMON_FIELD_MAPPING: ClassVar[dict[str, str]] = {
        "prettify_markdown": "prettifyMarkdown",
        "show_formula_number": "showFormulaNumber",
        "visualize": "visualize",
    }

    _ALGORITHM_FIELD_MAPPINGS: ClassVar[dict[str, dict[str, str]]] = {
        "PaddleOCR-VL": {
            "use_doc_orientation_classify": "useDocOrientationClassify",
            "use_doc_unwarping": "useDocUnwarping",
            "use_layout_detection": "useLayoutDetection",
            "use_chart_recognition": "useChartRecognition",
            "use_seal_recognition": "useSealRecognition",
            "use_ocr_for_image_block": "useOcrForImageBlock",
            "layout_threshold": "layoutThreshold",
            "layout_nms": "layoutNms",
            "layout_unclip_ratio": "layoutUnclipRatio",
            "layout_merge_bboxes_mode": "layoutMergeBboxesMode",
            "layout_shape_mode": "layoutShapeMode",
            "prompt_label": "promptLabel",
            "format_block_content": "formatBlockContent",
            "repetition_penalty": "repetitionPenalty",
            "temperature": "temperature",
            "top_p": "topP",
            "min_pixels": "minPixels",
            "max_pixels": "maxPixels",
            "max_new_tokens": "maxNewTokens",
            "merge_layout_blocks": "mergeLayoutBlocks",
            "markdown_ignore_labels": "markdownIgnoreLabels",
            "vlm_extra_args": "vlmExtraArgs",
            "restructure_pages": "restructurePages",
            "merge_tables": "mergeTables",
            "relevel_titles": "relevelTitles",
        },
        "PP-OCRv5": {f.name: _api_field_name(f.name) for f in fields(PaddleOCRTextConfig)},
        "PP-StructureV3": {f.name: _api_field_name(f.name) for f in fields(PaddleOCRStructureConfig)},
    }
    _ALGORITHM_FIELD_MAPPINGS["PaddleOCR-VL-1.5"] = _ALGORITHM_FIELD_MAPPINGS["PaddleOCR-VL"]

    def __init__(
        self,
        api_url: str | None = None,
        access_token: str | None = None,
        algorithm: AlgorithmType = "PaddleOCR-VL",
        *,
        request_timeout: int = 600,
        algorithm_config: dict[str, Any] | None = None,
    ) -> None:
        """Initialize PaddleOCR parser."""
        # Remote OCR does not use DeepDOC's local OCR/layout/table models.
        config = PaddleOCRConfig.from_kwargs(
            api_url=api_url if api_url is not None else os.getenv("PADDLEOCR_API_URL", ""),
            access_token=access_token if access_token is not None else os.getenv("PADDLEOCR_ACCESS_TOKEN"),
            algorithm=algorithm,
            request_timeout=request_timeout,
            algorithm_config=algorithm_config,
        )

        self.outlines = []
        self.api_url = config.api_url.rstrip("/")
        self.access_token = config.access_token
        self.algorithm = algorithm
        self.request_timeout = request_timeout
        self.algorithm_config = config.algorithm_config
        self.logger = logging.getLogger(self.__class__.__name__)

        # Force PDF file type
        self.file_type = 0

        # Initialize page images for cropping
        self.page_images: list[Image.Image] = []
        self.page_from = 0

    # Public methods
    def check_installation(self) -> tuple[bool, str]:
        """Check if the parser is properly installed and configured."""
        if not self.api_url:
            return False, "[PaddleOCR] API URL not configured"

        # This checks configuration only; inference is verified when parsing.

        return True, ""

    def parse_pdf(
        self,
        filepath: str | PathLike[str],
        binary: BytesIO | bytes | None = None,
        callback: Callable[[float, str], None] | None = None,
        *,
        parse_method: str = "raw",
        api_url: str | None = None,
        access_token: str | None = None,
        algorithm: AlgorithmType | None = None,
        request_timeout: int | None = None,
        prettify_markdown: bool | None = None,
        show_formula_number: bool | None = None,
        visualize: bool | None = None,
        additional_params: dict[str, Any] | None = None,
        algorithm_config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ParseResult:
        """Parse PDF document using PaddleOCR API."""
        self.outlines = extract_pdf_outlines(binary if binary is not None else filepath)
        # Create configuration - pass all kwargs to capture VL config parameters
        config_dict = {
            "api_url": api_url if api_url is not None else self.api_url,
            "access_token": access_token if access_token is not None else self.access_token,
            "algorithm": algorithm if algorithm is not None else self.algorithm,
            "request_timeout": request_timeout if request_timeout is not None else self.request_timeout,
        }
        if prettify_markdown is not None:
            config_dict["prettify_markdown"] = prettify_markdown
        if show_formula_number is not None:
            config_dict["show_formula_number"] = show_formula_number
        if visualize is not None:
            config_dict["visualize"] = visualize
        if additional_params is not None:
            config_dict["additional_params"] = additional_params
        config_dict["algorithm_config"] = algorithm_config if algorithm_config is not None else (self.algorithm_config if config_dict["algorithm"] == self.algorithm else {})

        cfg = PaddleOCRConfig.from_dict(config_dict)

        if not cfg.api_url:
            raise RuntimeError("[PaddleOCR] API URL missing")

        # Prepare file data and generate page images for cropping
        data_bytes = self._prepare_file_data(filepath, binary)

        # Generate page images for cropping functionality
        input_source = filepath if binary is None else binary
        try:
            self.__images__(input_source, callback=callback)
        except Exception as e:
            self.logger.warning(f"[PaddleOCR] Failed to generate page images for cropping: {e}")

        # Build and send request
        result = self._send_request(data_bytes, cfg, callback)

        # Process response
        sections = self._transfer_to_sections(result, algorithm=cfg.algorithm, parse_method=parse_method)
        if callback:
            callback(0.9, f"[PaddleOCR] done, sections: {len(sections)}")

        tables = self._transfer_to_tables(result)
        if callback:
            callback(1.0, f"[PaddleOCR] done, tables: {len(tables)}")

        return sections, tables

    def _prepare_file_data(self, filepath: str | PathLike[str], binary: BytesIO | bytes | None) -> bytes:
        """Prepare file data for API request."""
        source_path = Path(filepath)

        if binary is not None:
            if isinstance(binary, (bytes, bytearray)):
                return binary
            return binary.getbuffer().tobytes()

        if not source_path.exists():
            raise FileNotFoundError(f"[PaddleOCR] file not found: {source_path}")

        return source_path.read_bytes()

    def _build_payload(self, data: bytes, file_type: int, config: PaddleOCRConfig) -> dict[str, Any]:
        """Build payload for API request."""
        payload: dict[str, Any] = {
            "file": base64.b64encode(data).decode("ascii"),
            "fileType": file_type,
        }

        # Add common parameters
        for param_key, param_value in [
            ("prettify_markdown", config.prettify_markdown),
            ("show_formula_number", config.show_formula_number),
            ("visualize", config.visualize),
        ]:
            if config.algorithm == "PP-OCRv5" and param_key != "visualize":
                continue
            if param_value is not None:
                api_param = self._COMMON_FIELD_MAPPING[param_key]
                payload[api_param] = param_value

        # Add algorithm-specific parameters
        algorithm_mapping = self._ALGORITHM_FIELD_MAPPINGS.get(config.algorithm, {})
        for param_key, param_value in config.algorithm_config.items():
            if param_value is not None and param_key in algorithm_mapping:
                api_param = algorithm_mapping[param_key]
                payload[api_param] = param_value

        # Add any additional parameters
        if config.additional_params:
            payload.update(config.additional_params)

        return payload

    def _send_request(self, data: bytes, config: PaddleOCRConfig, callback: Callable[[float, str], None] | None) -> dict[str, Any]:
        """Send request to PaddleOCR API and parse response."""
        if _uses_job_api(config.api_url):
            return self._send_job_request(data, config, callback)
        # Build payload
        payload = self._build_payload(data, self.file_type, config)

        # Prepare headers
        headers = {"Content-Type": "application/json", "Client-Platform": "ragflow"}
        if config.access_token:
            headers["Authorization"] = f"token {config.access_token}"

        self.logger.info("[PaddleOCR] invoking API")
        if callback:
            callback(0.1, "[PaddleOCR] submitting request")

        # Send request
        try:
            resp = requests.post(config.api_url, json=payload, headers=headers, timeout=config.request_timeout)
            resp.raise_for_status()
        except Exception as exc:
            if callback:
                callback(-1, f"[PaddleOCR] request failed: {exc}")
            raise RuntimeError(f"[PaddleOCR] request failed: {exc}")

        # Parse response
        try:
            response_data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"[PaddleOCR] response is not JSON: {exc}") from exc

        if callback:
            callback(0.8, "[PaddleOCR] response received")

        # Validate response format
        try:
            return _validated_inference_result(response_data)
        except RuntimeError:
            if callback:
                callback(-1, "[PaddleOCR] invalid response format")
            raise

    @staticmethod
    def _job_data(response: requests.Response, phase: str) -> dict[str, Any]:
        if response.status_code != 200:
            try:
                error = response.json()
            except ValueError:
                error = None
            code = error.get("code") if isinstance(error, dict) else None
            detail = f" (API code {code})" if type(code) is int and code != 0 else ""
            raise RuntimeError(f"[PaddleOCR] {phase} failed: HTTP {response.status_code}{detail}")
        try:
            envelope = response.json()
        except ValueError as exc:
            raise RuntimeError(f"[PaddleOCR] {phase} response is not JSON") from exc
        if not isinstance(envelope, dict) or type(envelope.get("code")) is not int or not isinstance(envelope.get("data"), dict):
            raise RuntimeError(f"[PaddleOCR] invalid {phase} response")
        if envelope["code"] != 0:
            raise RuntimeError(f"[PaddleOCR] {phase} failed: API code {envelope['code']}")
        return envelope["data"]

    @staticmethod
    def _merge_job_results(jsonl: str, algorithm: AlgorithmType) -> dict[str, Any]:
        result_key = "ocrResults" if algorithm == "PP-OCRv5" else "layoutParsingResults"
        pages: list[dict[str, Any]] = []
        records = 0
        for line in jsonl.splitlines():
            if not line.strip():
                continue
            try:
                result = _validated_inference_result(json.loads(line))
            except ValueError as exc:
                raise RuntimeError("[PaddleOCR] result is not valid JSONL") from exc
            batch = result.get(result_key)
            if not isinstance(batch, list):
                raise RuntimeError(f"[PaddleOCR] job result missing {result_key} array")
            pages.extend(batch)
            records += 1
        if not records:
            raise RuntimeError("[PaddleOCR] empty job result JSONL")
        return {result_key: pages}

    def _send_job_request(self, data: bytes, config: PaddleOCRConfig, callback: Callable[[float, str], None] | None) -> dict[str, Any]:
        deadline = time.monotonic() + config.request_timeout

        def remaining() -> float:
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise RuntimeError(f"[PaddleOCR] job timed out after {config.request_timeout}s")
            return seconds

        payload = self._build_payload(b"", self.file_type, config)
        payload.pop("file")
        payload.pop("fileType")
        headers = {"Authorization": f"Bearer {config.access_token}", "Client-Platform": "ragflow"}
        if callback:
            callback(0.1, "[PaddleOCR] submitting job")
        try:
            response = requests.post(
                config.api_url,
                headers=headers,
                data={"model": config.algorithm, "optionalPayload": json.dumps(payload)},
                files={"file": ("document.pdf", data, "application/pdf")},
                timeout=remaining(),
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"[PaddleOCR] job submission request failed: {type(exc).__name__}") from None
        job_id = self._job_data(response, "submit").get("jobId")
        if not isinstance(job_id, str) or not job_id:
            raise RuntimeError("[PaddleOCR] submit response missing jobId")
        parsed_url = urlsplit(config.api_url)
        poll_url = urlunsplit(parsed_url._replace(path=parsed_url.path.rstrip("/") + "/" + quote(job_id, safe="")))
        if callback:
            callback(0.2, "[PaddleOCR] job submitted")
        interval = 3.0
        while True:
            try:
                response = requests.get(poll_url, headers=headers, timeout=remaining())
            except requests.RequestException as exc:
                raise RuntimeError(f"[PaddleOCR] job status request failed: {type(exc).__name__}") from None
            status = self._job_data(response, "poll")
            state = status.get("state")
            if state == "done":
                break
            if state == "failed":
                raise RuntimeError("[PaddleOCR] inference job failed")
            if not isinstance(state, str) or state not in {"pending", "running"}:
                raise RuntimeError("[PaddleOCR] invalid job state")
            time.sleep(min(interval, remaining()))
            interval = min(interval * 1.5, 15.0)
        result_urls = status.get("resultUrl")
        result_url = status.get("resultJsonUrl") or (result_urls.get("jsonUrl") if isinstance(result_urls, dict) else None)
        if not isinstance(result_url, str) or urlsplit(result_url).scheme not in {"http", "https"} or not urlsplit(result_url).hostname:
            raise RuntimeError("[PaddleOCR] done job missing valid result URL")
        if callback:
            callback(0.7, "[PaddleOCR] downloading job result")
        try:
            # Result links may point to object storage; never forward the API token.
            response = requests.get(result_url, timeout=remaining())
            if response.status_code != 200:
                raise RuntimeError(f"[PaddleOCR] result download failed: HTTP {response.status_code}")
        except requests.RequestException as exc:
            raise RuntimeError(f"[PaddleOCR] result download request failed: {type(exc).__name__}") from None
        return self._merge_job_results(response.text, config.algorithm)

    def _transfer_to_sections(self, result: dict[str, Any], algorithm: AlgorithmType, parse_method: str) -> list[SectionTuple]:
        """Convert API response to section tuples."""
        sections: list[SectionTuple] = []

        _algorithm_defaults(algorithm)
        result_key = "ocrResults" if algorithm == "PP-OCRv5" else "layoutParsingResults"
        page_results = result.get(result_key)
        if not isinstance(page_results, list):
            raise ValueError(f"[PaddleOCR] {algorithm} response missing {result_key} array")

        for page_idx, page_result in enumerate(page_results):
            if not isinstance(page_result, dict) or not isinstance(page_result.get("prunedResult"), dict):
                raise ValueError(f"[PaddleOCR] invalid {result_key} page result")
            pruned_result = page_result["prunedResult"]
            if algorithm == "PP-OCRv5":
                blocks = self._ocr_blocks(pruned_result)
            else:
                blocks = pruned_result.get("parsing_res_list")
                if not isinstance(blocks, list):
                    raise ValueError("[PaddleOCR] response missing parsing_res_list array")

            for block in blocks:
                if not isinstance(block, dict) or not isinstance(block.get("block_content"), str):
                    raise ValueError("[PaddleOCR] invalid text block")
                block_content = block.get("block_content", "").strip()
                if not block_content:
                    continue

                # Remove images
                block_content = _remove_images_from_markdown(block_content)

                label = block.get("block_label", "")
                block_bbox = block.get("block_bbox", [0, 0, 0, 0])
                left, top, right, bottom = _normalize_bbox(block_bbox)

                tag = f"@@{page_idx + 1}\t{left // self._ZOOMIN}\t{right // self._ZOOMIN}\t{top // self._ZOOMIN}\t{bottom // self._ZOOMIN}##"

                if parse_method in {"manual", "pipeline"}:
                    sections.append((block_content, label, tag))
                elif parse_method == "paper":
                    sections.append((block_content + tag, label))
                else:
                    sections.append((block_content, tag))

        return sections

    @staticmethod
    def _ocr_blocks(pruned_result: dict[str, Any]) -> list[dict[str, Any]]:
        texts = pruned_result.get("rec_texts")
        if not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
            raise ValueError("[PaddleOCR] response missing rec_texts string array")
        boxes = pruned_result.get("rec_boxes", [])
        polygons = pruned_result.get("rec_polys", [])
        if not isinstance(boxes, list) or not isinstance(polygons, list):
            raise ValueError("[PaddleOCR] invalid OCR coordinates")
        blocks: list[dict[str, Any]] = []
        for index, text in enumerate(texts):
            bbox = boxes[index] if index < len(boxes) else [0, 0, 0, 0]
            if index >= len(boxes) and index < len(polygons):
                points = polygons[index]
                if not isinstance(points, list) or not points or any(not isinstance(point, list) or len(point) != 2 for point in points):
                    raise ValueError("[PaddleOCR] invalid OCR polygon")
                xs, ys = zip(*points)
                bbox = [min(xs), min(ys), max(xs), max(ys)]
            blocks.append({"block_content": text, "block_label": "text", "block_bbox": bbox})
        return blocks

    def _transfer_to_tables(self, result: dict[str, Any]) -> list[TableTuple]:
        """Convert API response to table tuples."""
        return []

    def __images__(self, fnm: str | bytes | PathLike[str], page_from: int = 0, page_to: int = MAXIMUM_PAGE_NUMBER, callback: Callable[..., Any] | None = None) -> None:
        """Generate page images from PDF for cropping."""
        self.page_from = page_from
        self.page_to = page_to
        try:
            with pdfplumber.open(fnm) if isinstance(fnm, (str, PathLike)) else pdfplumber.open(BytesIO(fnm)) as pdf:
                self.pdf = pdf
                self.page_images = [p.to_image(resolution=72, antialias=True).original for i, p in enumerate(self.pdf.pages[page_from:page_to])]
        except Exception as e:
            self.page_images = None
            self.logger.exception(e)

    @staticmethod
    def extract_positions(txt: str):
        """Extract position information from text tags."""
        poss = []
        for tag in re.findall(r"@@[0-9-]+\t[0-9.\t]+##", txt):
            pn, left, right, top, bottom = tag.strip("#").strip("@").split("\t")
            left, right, top, bottom = float(left), float(right), float(top), float(bottom)
            poss.append(([int(p) - 1 for p in pn.split("-")], left, right, top, bottom))
        return poss

    def crop(self, text: str, need_position: bool = False):
        """Crop images from PDF based on position tags in text."""
        imgs = []
        poss = self.extract_positions(text)

        if not poss:
            if need_position:
                return None, None
            return

        if not getattr(self, "page_images", None):
            self.logger.warning("[PaddleOCR] crop called without page images; skipping image generation.")
            if need_position:
                return None, None
            return

        page_count = len(self.page_images)

        filtered_poss = []
        for pns, left, right, top, bottom in poss:
            if not pns:
                self.logger.warning("[PaddleOCR] Empty page index list in crop; skipping this position.")
                continue
            valid_pns = [p for p in pns if 0 <= p < page_count]
            if not valid_pns:
                self.logger.warning(f"[PaddleOCR] All page indices {pns} out of range for {page_count} pages; skipping.")
                continue
            filtered_poss.append((valid_pns, left, right, top, bottom))

        poss = filtered_poss
        if not poss:
            self.logger.warning("[PaddleOCR] No valid positions after filtering; skip cropping.")
            if need_position:
                return None, None
            return

        max_width = max(np.max([right - left for (_, left, right, _, _) in poss]), 6)
        GAP = 6
        pos = poss[0]
        first_page_idx = pos[0][0]
        poss.insert(0, ([first_page_idx], pos[1], pos[2], max(0, pos[3] - 120), max(pos[3] - GAP, 0)))
        pos = poss[-1]
        last_page_idx = pos[0][-1]
        if not (0 <= last_page_idx < page_count):
            self.logger.warning(f"[PaddleOCR] Last page index {last_page_idx} out of range for {page_count} pages; skipping crop.")
            if need_position:
                return None, None
            return
        last_page_height = self.page_images[last_page_idx].size[1]
        poss.append(
            (
                [last_page_idx],
                pos[1],
                pos[2],
                min(last_page_height, pos[4] + GAP),
                min(last_page_height, pos[4] + 120),
            )
        )

        positions = []
        for ii, (pns, left, right, top, bottom) in enumerate(poss):
            right = left + max_width

            if bottom <= top:
                bottom = top + 2

            for pn in pns[1:]:
                if 0 <= pn - 1 < page_count:
                    bottom += self.page_images[pn - 1].size[1]
                else:
                    self.logger.warning(f"[PaddleOCR] Page index {pn}-1 out of range for {page_count} pages during crop; skipping height accumulation.")

            if not (0 <= pns[0] < page_count):
                self.logger.warning(f"[PaddleOCR] Base page index {pns[0]} out of range for {page_count} pages during crop; skipping this segment.")
                continue

            img0 = self.page_images[pns[0]]
            x0, y0, x1, y1 = int(left), int(top), int(right), int(min(bottom, img0.size[1]))
            if x0 > x1:
                x0, x1 = x1, x0
            if y0 > y1:
                y0, y1 = y1, y0
            x0 = max(0, min(x0, img0.size[0]))
            x1 = max(0, min(x1, img0.size[0]))
            y0 = max(0, min(y0, img0.size[1]))
            y1 = max(0, min(y1, img0.size[1]))
            if x1 <= x0 or y1 <= y0:
                continue
            crop0 = img0.crop((x0, y0, x1, y1))
            imgs.append(crop0)
            if 0 < ii < len(poss) - 1:
                positions.append((pns[0] + self.page_from, x0, x1, y0, y1))

            bottom -= img0.size[1]
            for pn in pns[1:]:
                if not (0 <= pn < page_count):
                    self.logger.warning(f"[PaddleOCR] Page index {pn} out of range for {page_count} pages during crop; skipping this page.")
                    continue
                page = self.page_images[pn]
                x0, y0, x1, y1 = int(left), 0, int(right), int(min(bottom, page.size[1]))
                if x0 > x1:
                    x0, x1 = x1, x0
                if y0 > y1:
                    y0, y1 = y1, y0
                x0 = max(0, min(x0, page.size[0]))
                x1 = max(0, min(x1, page.size[0]))
                y0 = max(0, min(y0, page.size[1]))
                y1 = max(0, min(y1, page.size[1]))
                if x1 <= x0 or y1 <= y0:
                    bottom -= page.size[1]
                    continue
                cimgp = page.crop((x0, y0, x1, y1))
                imgs.append(cimgp)
                if 0 < ii < len(poss) - 1:
                    positions.append((pn + self.page_from, x0, x1, y0, y1))
                bottom -= page.size[1]

        if not imgs:
            if need_position:
                return None, None
            return

        total_height = 0
        max_width = 0
        img_sizes = []
        for img in imgs:
            w, h = img.size
            img_sizes.append((w, h))
            max_width = max(max_width, w)
            total_height += h + GAP

        pic = Image.new("RGB", (max_width, int(total_height)), (245, 245, 245))
        current_height = 0
        imgs_count = len(imgs)
        for ii, (img, (w, h)) in enumerate(zip(imgs, img_sizes)):
            if ii == 0 or ii + 1 == imgs_count:
                img = img.convert("RGBA")
                overlay = Image.new("RGBA", img.size, (0, 0, 0, 128))
                img = Image.alpha_composite(img, overlay).convert("RGB")
            pic.paste(img, (0, int(current_height)))
            current_height += h + GAP

        if need_position:
            return pic, positions
        return pic


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = PaddleOCRParser(api_url=os.getenv("PADDLEOCR_API_URL", ""), algorithm=os.getenv("PADDLEOCR_ALGORITHM", "PaddleOCR-VL"))
    ok, reason = parser.check_installation()
    print("PaddleOCR available:", ok, reason)
