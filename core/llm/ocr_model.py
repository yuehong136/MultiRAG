#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
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
import json
import logging
import os
from collections.abc import Callable
from io import BytesIO
from typing import Any, cast

from deepdoc.parser.mineru_parser import MinerUParser
from deepdoc.parser.opendataloader_parser import OpenDataLoaderParser
from deepdoc.parser.paddleocr_parser import AlgorithmType, PaddleOCRParser, ParseResult


class Base:
    def __init__(self, key: str | dict, model_name: str, **kwargs):
        self.model_name = model_name

    def parse_pdf(self, filepath: str, binary=None, **kwargs) -> tuple[Any, Any]:
        raise NotImplementedError("Please implement parse_pdf!")


class MinerUOcrModel(Base, MinerUParser):
    _FACTORY_NAME = "MinerU"

    def __init__(self, key: str | dict, model_name: str, **kwargs):
        Base.__init__(self, key, model_name, **kwargs)
        config = {}
        if key:
            try:
                config = json.loads(key)
            except Exception:
                config = {}
        config = config["api_key"]
        self.mineru_api = config.get("mineru_apiserver", os.environ.get("MINERU_APISERVER", ""))
        self.mineru_output_dir = config.get("mineru_output_dir", os.environ.get("MINERU_OUTPUT_DIR", ""))
        self.mineru_backend = config.get("mineru_backend", os.environ.get("MINERU_BACKEND", "pipeline"))
        self.mineru_server_url = config.get("mineru_server_url", os.environ.get("MINERU_SERVER_URL", ""))
        self.mineru_delete_output = bool(int(config.get("mineru_delete_output", os.environ.get("MINERU_DELETE_OUTPUT", 1))))

        # Redact sensitive config keys before logging
        redacted_config = {}
        for k, v in config.items():
            if any(sensitive_word in k.lower() for sensitive_word in ("key", "password", "token", "secret")):
                redacted_config[k] = "[REDACTED]"
            else:
                redacted_config[k] = v
        logging.info(f"Parsed MinerU config (sensitive fields redacted): {redacted_config}")

        MinerUParser.__init__(self, mineru_api=self.mineru_api, mineru_server_url=self.mineru_server_url)

    def check_available(self, backend: str | None = None, server_url: str | None = None) -> tuple[bool, str]:
        backend = backend or self.mineru_backend
        server_url = server_url or self.mineru_server_url
        return self.check_installation(backend=backend, server_url=server_url)

    def parse_pdf(self, filepath: str, binary=None, callback=None, parse_method: str = "raw", **kwargs):
        ok, reason = self.check_available()
        if not ok:
            raise RuntimeError(f"MinerU server not accessible: {reason}")

        sections, tables = MinerUParser.parse_pdf(
            self,
            filepath=filepath,
            binary=binary,
            callback=callback,
            output_dir=self.mineru_output_dir,
            backend=self.mineru_backend,
            server_url=self.mineru_server_url,
            delete_output=self.mineru_delete_output,
            parse_method=parse_method,
            **kwargs,
        )
        return sections, tables


class PaddleOCROcrModel(Base, PaddleOCRParser):
    _FACTORY_NAME = "PaddleOCR"

    def __init__(self, key: str | dict, model_name: str, **kwargs: Any) -> None:
        Base.__init__(self, key, model_name, **kwargs)
        raw_config: dict[str, Any] = {}
        if key:
            try:
                raw_config = json.loads(key) if isinstance(key, str) else key
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("PaddleOCR model configuration must be a JSON object") from exc
        if not isinstance(raw_config, dict):
            raise ValueError("PaddleOCR model configuration must be an object")

        # nested {"api_key": {...}} from UI
        # flat {"PADDLEOCR_*": "..."} payload auto-provisioned from env vars
        config = raw_config.get("api_key", raw_config)
        if not isinstance(config, dict):
            raise ValueError("PaddleOCR api_key configuration must be an object")

        def _resolve_config(key: str, env_key: str, default: Any = "") -> Any:
            # lower-case keys (UI), upper-case PADDLEOCR_* (env auto-provision), env vars
            return config.get(key, config.get(env_key, os.environ.get(env_key, default)))

        self.paddleocr_api_url = _resolve_config("paddleocr_api_url", "PADDLEOCR_API_URL", "")
        self.paddleocr_algorithm = _resolve_config("paddleocr_algorithm", "PADDLEOCR_ALGORITHM", "PaddleOCR-VL")
        self.paddleocr_access_token = _resolve_config("paddleocr_access_token", "PADDLEOCR_ACCESS_TOKEN", None)

        timeout = _resolve_config("paddleocr_timeout", "PADDLEOCR_TIMEOUT", 600)
        if isinstance(timeout, bool) or not isinstance(timeout, (str, int)):
            raise ValueError("PaddleOCR timeout must be a positive integer")
        try:
            request_timeout = int(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("PaddleOCR timeout must be a positive integer") from exc
        algorithm_config = config.get("paddleocr_algorithm_config")

        PaddleOCRParser.__init__(
            self,
            api_url=self.paddleocr_api_url,
            access_token=self.paddleocr_access_token,
            algorithm=cast(AlgorithmType, self.paddleocr_algorithm),
            request_timeout=request_timeout,
            algorithm_config=algorithm_config,
        )

    def check_available(self) -> tuple[bool, str]:
        return self.check_installation()

    def parse_pdf(
        self,
        filepath: str,
        binary: BytesIO | bytes | None = None,
        callback: Callable[[float, str], None] | None = None,
        parse_method: str = "raw",
        **kwargs: Any,
    ) -> ParseResult:
        ok, reason = self.check_available()
        if not ok:
            raise RuntimeError(f"PaddleOCR server not accessible: {reason}")

        sections, tables = PaddleOCRParser.parse_pdf(self, filepath=filepath, binary=binary, callback=callback, parse_method=parse_method, **kwargs)
        return sections, tables


class OpenDataLoaderOcrModel(Base, OpenDataLoaderParser):
    _FACTORY_NAME = "OpenDataLoader"

    def __init__(self, key: str | dict, model_name: str, **kwargs: Any) -> None:
        Base.__init__(self, key, model_name, **kwargs)
        raw_config: dict[str, Any] = {}
        if key:
            try:
                raw_config = json.loads(key) if isinstance(key, str) else key
            except (TypeError, json.JSONDecodeError):
                raw_config = {}

        config = raw_config.get("api_key", raw_config)
        if not isinstance(config, dict):
            config = {}

        def resolve_config(config_key: str, env_key: str, default: Any = "") -> Any:
            return config.get(config_key, config.get(env_key, os.environ.get(env_key, default)))

        redacted_config = {config_key: "[REDACTED]" if any(sensitive in config_key.lower() for sensitive in ("key", "password", "token", "secret")) else value for config_key, value in config.items()}
        logging.info("Parsed OpenDataLoader config (sensitive fields redacted): %s", redacted_config)

        OpenDataLoaderParser.__init__(self)
        self.api_url = str(resolve_config("opendataloader_apiserver", "OPENDATALOADER_APISERVER", "")).rstrip("/")
        self.api_key = str(resolve_config("opendataloader_api_key", "OPENDATALOADER_API_KEY", "")).strip()
        timeout_value = resolve_config("opendataloader_timeout", "OPENDATALOADER_TIMEOUT", "600") or "600"
        try:
            self.timeout = int(timeout_value)
        except (TypeError, ValueError):
            self.timeout = 600

    def check_available(self) -> tuple[bool, str]:
        available = self.check_installation()
        return available, "" if available else "OpenDataLoader service not reachable"

    def parse_pdf(
        self,
        filepath: str,
        binary: Any = None,
        callback: Any = None,
        parse_method: str = "raw",
        **kwargs: Any,
    ) -> tuple[Any, Any]:
        available, reason = self.check_available()
        if not available:
            raise RuntimeError(f"OpenDataLoader service not accessible: {reason}")

        return OpenDataLoaderParser.parse_pdf(
            self,
            filepath=filepath,
            binary=binary,
            callback=callback,
            parse_method=parse_method,
            **kwargs,
        )
