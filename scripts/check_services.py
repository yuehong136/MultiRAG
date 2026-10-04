"""Read-only protocol readiness using the same probes as the integration runner."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.support.services import ServiceError, ServiceManager


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("services", nargs="*", metavar="SERVICE", help="postgresql, redis, minio, milvus or infinity (default: first three)")
    args = parser.parse_args(argv)
    names = args.services or ["postgresql", "redis", "minio"]
    if set(names) - {"postgresql", "redis", "minio", "milvus", "infinity"}:
        parser.error("Unknown service; choose postgresql, redis, minio, milvus or infinity")
    from common.config_utils import CONFIGS

    try:
        with ServiceManager(CONFIGS, allow_containers=False) as manager:
            for name in names:
                manager.ensure(name)
    except ServiceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
