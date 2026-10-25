"""Task executor command line contract, independent of business initialization."""

import argparse
from collections.abc import Sequence


def parse_worker_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MultiRAG task executor")
    parser.add_argument("-i", "--index", default="0", help="Worker index or unique host_index identity")
    parser.add_argument("-t", "--type", default="common", choices=("common",), help="Shared task queue; task-specific queues are not supported")
    return parser.parse_args(argv)
