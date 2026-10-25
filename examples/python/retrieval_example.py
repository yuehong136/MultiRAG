"""Semantic and keyword retrieval on a populated dataset; no resource writes."""

from common import request, required, show


def main() -> None:
    for keyword in (False, True):
        show(
            request(
                "POST",
                "/retrieval",
                {"dataset_ids": [required("MULTIRAG_DATASET_ID")], "question": "retrieval workflow", "page": 1, "page_size": 5, "similarity_threshold": 0, "top_k": 10, "keyword": keyword},
            )
        )


if __name__ == "__main__":
    main()
