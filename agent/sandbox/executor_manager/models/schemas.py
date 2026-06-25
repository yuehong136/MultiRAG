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
import base64
from typing import Any

from pydantic import BaseModel, Field, field_validator

from models.enums import ResourceLimitType, ResultStatus, RuntimeErrorType, SupportLanguage, UnauthorizedAccessType


class ArtifactItem(BaseModel):
    name: str
    mime_type: str
    size: int
    content_b64: str


class ExecutionStructuredResult(BaseModel):
    present: bool
    value: Any = None
    type: str = "json"


class CodeExecutionResult(BaseModel):
    status: ResultStatus
    stdout: str
    stderr: str
    exit_code: int
    detail: str | None = None

    # Resource usage
    time_used_ms: float | None = None
    memory_used_kb: float | None = None

    # Error details
    resource_limit_type: ResourceLimitType | None = None
    unauthorized_access_type: UnauthorizedAccessType | None = None
    runtime_error_type: RuntimeErrorType | None = None

    # File artifacts produced by code execution (images, PDFs, CSVs, etc.)
    artifacts: list[ArtifactItem] = []

    # Structured return value produced by main()
    result: ExecutionStructuredResult | None = None


class CodeExecutionRequest(BaseModel):
    code_b64: str = Field(..., description="Base64 encoded code string")
    language: SupportLanguage = Field(default=SupportLanguage.PYTHON, description="Programming language")
    arguments: dict | None = Field(default={}, description="Arguments")

    @field_validator("code_b64")
    @classmethod
    def validate_base64(cls, v: str) -> str:
        try:
            base64.b64decode(v, validate=True)
            return v
        except Exception as e:
            raise ValueError(f"Invalid base64 encoding: {str(e)}")
