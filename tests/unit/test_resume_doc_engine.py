from unittest.mock import Mock

import pytest

from core.app import resume


def test_resume_rejects_non_elasticsearch_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOC_ENGINE", "milvus")
    parse_resume = Mock(side_effect=AssertionError("resume parsing must not start"))
    callback = Mock()
    monkeypatch.setattr(resume, "parse_resume", parse_resume)

    with pytest.raises(Exception, match=r"Resume is supported only with Elasticsearch\."):
        resume.chunk("resume.pdf", b"", "tenant-1", callback=callback)

    parse_resume.assert_not_called()
    callback.assert_not_called()
