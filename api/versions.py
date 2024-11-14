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
import subprocess


def get_versions() -> typing.Mapping[str, typing.Any]:
    dotenv.load_dotenv(dotenv.find_dotenv())
    return dotenv.dotenv_values()


def get_rag_version() -> typing.Optional[str]:
    return get_versions().get("MULITIRAG_IMAGE", "dev")


MULITIRAG_VERSION_INFO = "dev"


def get_closest_tag_and_count():
    # Get the current commit hash
    commit_id = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD']).strip().decode('utf-8')
    # Get the closest tag
    closest_tag = subprocess.check_output(['git', 'describe', '--tags', '--abbrev=0']).strip().decode('utf-8')
    # Get the commit hash of the closest tag
    closest_tag_commit = subprocess.check_output(['git', 'rev-list', '-n', '1', closest_tag]).strip().decode('utf-8')
    # Get the commit count since the closest tag
    process = subprocess.Popen(['git', 'rev-list', '--count', f'{closest_tag}..HEAD'], stdout=subprocess.PIPE)
    commits_count, _ = process.communicate()
    commits_count = int(commits_count.strip())

    if commits_count == 0:
        return closest_tag
    else:
        return f"{commit_id}({closest_tag}~{commits_count})"


if MULITIRAG_VERSION_INFO == 'dev':
    MULITIRAG_VERSION_INFO = get_closest_tag_and_count()
