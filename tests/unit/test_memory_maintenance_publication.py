"""Only reviewed candidate identities, never replacement bodies, cross this boundary."""

from copy import deepcopy

import pytest

from memory.maintenance.publication import publication_selections
from memory.revisions.domain import RevisionValidationError


def test_publication_selection_is_canonical_and_does_not_mutate_the_request() -> None:
    selected = [{"candidate_id": "z", "expected_revision": 3}, {"candidate_id": "a", "expected_revision": 2}]
    original = deepcopy(selected)
    result = publication_selections(selected)
    assert [item["candidate_id"] for item in result] == ["a", "z"]
    result[0]["expected_revision"] = 20
    assert selected == original


@pytest.mark.parametrize(
    "selected",
    [
        [],
        [{"candidate_id": "a", "expected_revision": True}],
        [{"candidate_id": "a", "expected_revision": 0}],
        [{"candidate_id": "a", "expected_revision": 2, "content": "unreviewed replacement"}],
        [{"candidate_id": "a", "expected_revision": 2}] * 2,
        [{"candidate_id": str(index), "expected_revision": 2} for index in range(17)],
    ],
)
def test_publication_refuses_unbounded_duplicate_or_freeform_selections(selected: list[dict[str, object]]) -> None:
    with pytest.raises(RevisionValidationError):
        publication_selections(selected)
