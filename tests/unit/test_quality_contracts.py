"""Scoring must reject superficially plausible but ungrounded model outputs."""

import pytest

from tests.evals.contracts import CORPUS, Corpus, Observation, generation_assertions, retrieval_scores


def test_checked_in_corpus_references_existing_sources() -> None:
    corpus = Corpus.model_validate_json((CORPUS / "cases.json").read_text())
    assert any(case.abstain for case in corpus.cases) and any(case.tools for case in corpus.cases)
    for case in corpus.cases:
        assert all((CORPUS / name).is_file() for name in case.documents)


def test_retrieval_deduplicates_results_and_penalizes_missing_second_source() -> None:
    case = next(case for case in Corpus.model_validate_json((CORPUS / "cases.json").read_text()).cases if case.id == "cross-document")
    result = Observation(retrieved=["wrong.md", "purchase.md", "purchase.md", "retention.md"])
    assert retrieval_scores(case, result) == {"recall_at_3": 0.5, "reciprocal_rank": 0.5}


def test_correct_fact_does_not_excuse_fabricated_citation() -> None:
    case = Corpus.model_validate_json((CORPUS / "cases.json").read_text()).cases[0]
    values = generation_assertions(case, Observation(answer="部门负责人", citations=["forged.md"]))
    assert values["required_facts"] is True
    assert values["citation_precision"] is False and values["citation_coverage"] is False


def test_unanswerable_case_requires_abstention_without_fabricated_sources() -> None:
    case = next(case for case in Corpus.model_validate_json((CORPUS / "cases.json").read_text()).cases if case.abstain)
    assert not generation_assertions(case, Observation(answer="收费 200 元"))["abstention"]
    assert not generation_assertions(case, Observation(abstained=True, citations=["retention.md"]))["citation_precision"]
    with pytest.raises(ValueError, match="positive relevance"):
        retrieval_scores(case, Observation())


def test_correct_tool_name_with_wrong_resource_id_fails() -> None:
    case = next(case for case in Corpus.model_validate_json((CORPUS / "cases.json").read_text()).cases if case.tools)
    result = Observation(answer="处理中", tool_calls=[{"name": "ticket_status", "arguments": {"ticket_id": "QH-9999"}}])
    values = generation_assertions(case, result)
    assert values["tool_selection"] is True and values["tool_arguments"] is False


def test_numeric_fact_cannot_match_a_different_number() -> None:
    case = next(case for case in Corpus.model_validate_json((CORPUS / "cases.json").read_text()).cases if "512" in case.facts)
    answer = " ".join(case.facts).replace("512", "5120")
    assert not generation_assertions(case, Observation(answer=answer))["required_facts"]
