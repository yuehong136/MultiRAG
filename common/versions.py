"""
@project: multirag
@Author：龙
@file： versions.py
@date：2024/7/26 13:49
@desc:
"""
import os
import subprocess

MULITIRAG_VERSION_INFO = "unknown"

def get_multirag_version() -> str:
    global MULITIRAG_VERSION_INFO
    if MULITIRAG_VERSION_INFO != "unknown":
        return MULITIRAG_VERSION_INFO
    version_path = os.path.abspath(
        os.path.join(
            os.path.dirname(os.path.realpath(__file__)), os.pardir, "VERSION"
        )
    )
    if os.path.exists(version_path):
        with open(version_path) as f:
            MULITIRAG_VERSION_INFO = f.read().strip()
    else:
        MULITIRAG_VERSION_INFO = get_closest_tag_and_count()
    return MULITIRAG_VERSION_INFO


def get_closest_tag_and_count():
    try:
        # Get the current commit hash
        version_info = (
            subprocess.check_output(["git", "describe", "--tags", "--match=v*", "--first-parent", "--always"])
            .strip()
            .decode("utf-8")
        )
        return version_info
    except Exception:
        return "unknown"
