"""Keep the SDK, lock and deployed Infinity server on the same release."""

import tomllib
from importlib.metadata import version
from pathlib import Path

import yaml
from packaging.requirements import Requirement
from packaging.version import Version


def test_infinity_sdk_lock_runtime_and_compose_match() -> None:
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    lock = tomllib.loads((root / "uv.lock").read_text())
    compose = yaml.safe_load((root / "docker/docker-compose-base.yml").read_text())
    requirement = next(Requirement(item) for item in project["project"]["dependencies"] if Requirement(item).name == "infinity-sdk")
    locked = next(package for package in lock["package"] if package["name"] == "infinity-sdk")
    image = compose["services"]["infinity"]["image"]
    repository, tag = image.rsplit(":", 1)
    assert repository == "infiniflow/infinity"
    assert len(requirement.specifier) == 1
    assert next(iter(requirement.specifier)).operator == "=="
    assert Version(tag.removeprefix("v")) == Version(locked["version"]) == Version(version("infinity-sdk"))
    assert requirement.specifier.contains(locked["version"], prereleases=True)
