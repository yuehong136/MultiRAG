# coding=utf-8
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
        with open(version_path, "r") as f:
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
        # commit_id = (
        #     subprocess.check_output(["git", "rev-parse", "--short", "HEAD"])
        #     .strip()
        #     .decode("utf-8")
        # )
        # # Get the closest tag
        # closest_tag = (
        #     subprocess.check_output(["git", "describe", "--tags", "--abbrev=0"])
        #     .strip()
        #     .decode("utf-8")
        # )
        # # Get the commit count since the closest tag
        # process = subprocess.Popen(
        #     ["git", "rev-list", "--count", f"{closest_tag}..HEAD"],
        #     stdout=subprocess.PIPE,
        # )
        # commits_count, _ = process.communicate()
        # commits_count = int(commits_count.strip())
        #
        # if commits_count == 0:
        #     return closest_tag
        # else:
        #     return f"{commit_id}({closest_tag}~{commits_count})"
    except Exception:
        return "unknown"

# import dotenv
# import typing

# def get_versions() -> typing.Mapping[str, typing.Any]:
#     dotenv.load_dotenv(dotenv.find_dotenv())
#     return dotenv.dotenv_values()
#
#
# def get_rag_version() -> typing.Optional[str]:
#     return get_versions().get("MULITIRAG_IMAGE", "dev")