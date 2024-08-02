# coding=utf-8
"""
@project: multirag
@Author：龙
@file： versions.py
@date：2024/7/26 13:49
@desc:
"""
import os
import dotenv
import typing
from api.utils.file_utils import get_project_base_directory


def get_versions() -> typing.Mapping[str, typing.Any]:
    dotenv.load_dotenv(dotenv.find_dotenv())
    return dotenv.dotenv_values()


def get_rag_version() -> typing.Optional[str]:
    return get_versions().get("MULITIRAG_VERSION", "dev")
