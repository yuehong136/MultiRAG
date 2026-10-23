"""HTML-to-PDF output and WebDriver ownership across success and failure."""

import base64
import binascii
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock, sentinel

import pytest
from selenium.common.exceptions import TimeoutException, WebDriverException
from sqlalchemy.orm import Session

from api.utils import web_utils

PDF = b"%PDF-1.4\nconversion result\n%%EOF\n"


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock, MagicMock]:
    driver = MagicMock(spec=web_utils.webdriver.Chrome)
    driver.execute_cdp_cmd.return_value = {"data": base64.b64encode(PDF).decode()}
    factory = MagicMock(return_value=driver)
    wait = MagicMock()
    monkeypatch.setattr(web_utils.webdriver, "Chrome", factory)
    monkeypatch.setattr(web_utils, "WebDriverWait", MagicMock(return_value=wait))
    monkeypatch.setattr(web_utils, "_managed_chromedriver_service", MagicMock(return_value=sentinel.service))
    return driver, factory, wait


@pytest.mark.parametrize("install_driver", [True, False])
@pytest.mark.parametrize("wait_times_out", [True, False])
def test_html2pdf_prints_and_quits_after_wait(browser: tuple[MagicMock, MagicMock, MagicMock], install_driver: bool, wait_times_out: bool) -> None:
    driver, factory, wait = browser
    if wait_times_out:
        wait.until.side_effect = TimeoutException("page stays attached")
    print_options = {"landscape": True, "scale": 0.75}

    assert web_utils.html2pdf("https://example.test/page", install_driver=install_driver, print_options=print_options) == PDF

    driver.quit.assert_called_once_with()
    driver.execute_cdp_cmd.assert_called_once_with("Page.printToPDF", {"landscape": True, "displayHeaderFooter": False, "printBackground": True, "preferCSSPageSize": True, "scale": 0.75})
    assert print_options == {"landscape": True, "scale": 0.75}
    if install_driver:
        assert factory.call_args.kwargs["service"] is sentinel.service
    else:
        assert "service" not in factory.call_args.kwargs


@pytest.mark.parametrize("stage", ["get", "find_element", "wait", "print", "missing_data", "decode"])
def test_html2pdf_quits_on_conversion_failure(browser: tuple[MagicMock, MagicMock, MagicMock], stage: str) -> None:
    driver, _, wait = browser
    error = WebDriverException(f"{stage} failed")
    if stage in {"get", "find_element"}:
        getattr(driver, stage).side_effect = error
    elif stage == "wait":
        wait.until.side_effect = error
    elif stage == "print":
        driver.execute_cdp_cmd.side_effect = error
    elif stage == "missing_data":
        driver.execute_cdp_cmd.return_value = {}
    else:
        driver.execute_cdp_cmd.return_value = {"data": "a"}
    expected_error = KeyError if stage == "missing_data" else binascii.Error if stage == "decode" else WebDriverException

    with pytest.raises(expected_error) as raised:
        web_utils.html2pdf("https://example.test/page")

    if expected_error is WebDriverException:
        assert raised.value is error
    driver.quit.assert_called_once_with()
    if stage in {"get", "find_element", "wait"}:
        driver.execute_cdp_cmd.assert_not_called()


@pytest.mark.parametrize("conversion_fails", [True, False])
def test_html2pdf_preserves_error_when_quit_fails(browser: tuple[MagicMock, MagicMock, MagicMock], caplog: pytest.LogCaptureFixture, conversion_fails: bool) -> None:
    driver, _, _ = browser
    conversion_error = WebDriverException("navigation failed")
    quit_error = WebDriverException("quit failed")
    driver.quit.side_effect = quit_error
    if conversion_fails:
        driver.get.side_effect = conversion_error

    with caplog.at_level(logging.ERROR), pytest.raises(WebDriverException) as raised:
        web_utils.html2pdf("https://example.test/page")

    assert raised.value is (conversion_error if conversion_fails else quit_error)
    driver.quit.assert_called_once_with()
    if conversion_fails:
        assert "Failed to quit WebDriver" in caplog.text
        assert caplog.records[-1].exc_info[1] is quit_error


def test_html2pdf_propagates_driver_creation_failure(browser: tuple[MagicMock, MagicMock, MagicMock]) -> None:
    driver, factory, _ = browser
    error = WebDriverException("session creation failed")
    factory.side_effect = error

    with pytest.raises(WebDriverException) as raised:
        web_utils.html2pdf("https://example.test/page")

    assert raised.value is error
    driver.quit.assert_not_called()


def test_html2pdf_does_not_print_after_navigation_timeout(browser: tuple[MagicMock, MagicMock, MagicMock]) -> None:
    driver, _, _ = browser
    error = TimeoutException("navigation timed out")
    driver.get.side_effect = error

    with pytest.raises(TimeoutException) as raised:
        web_utils.html2pdf("https://example.test/page")

    assert raised.value is error
    driver.execute_cdp_cmd.assert_not_called()
    driver.quit.assert_called_once_with()


def test_html2pdf_quits_on_interruption(browser: tuple[MagicMock, MagicMock, MagicMock]) -> None:
    driver, _, _ = browser
    error = KeyboardInterrupt()
    driver.get.side_effect = error

    with pytest.raises(KeyboardInterrupt) as raised:
        web_utils.html2pdf("https://example.test/page")

    assert raised.value is error
    driver.quit.assert_called_once_with()


@pytest.mark.parametrize("conversion_fails", [True, False])
def test_document_consumer_uploads_only_converted_pdf(browser: tuple[MagicMock, MagicMock, MagicMock], db: Session, monkeypatch: pytest.MonkeyPatch, conversion_fails: bool) -> None:
    from api.apps.services import document_api_service
    from api.db.db_models import Knowledgebase
    from api.db.services.file_service import FileService
    from api.db.services.knowledgebase_service import KnowledgebaseService

    driver, _, _ = browser
    kb = Knowledgebase(id="kb-pdf", tenant_id="owner")

    @contextmanager
    def connection() -> Iterator[Session]:
        yield db

    monkeypatch.setattr(document_api_service, "db_connection", connection)
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, session, dataset_id: kb))
    monkeypatch.setattr(document_api_service, "check_kb_team_permission", lambda session, dataset, tenant_id: True)
    monkeypatch.setattr(document_api_service, "is_valid_url", lambda url: True)
    upload = MagicMock(return_value=([], [({"id": "doc-pdf", "kb_id": kb.id, "name": "page.pdf"}, PDF)]))
    monkeypatch.setattr(FileService, "upload_document", upload)
    assert document_api_service.html2pdf is web_utils.html2pdf

    if conversion_fails:
        error = WebDriverException("conversion failed")
        driver.get.side_effect = error
        with pytest.raises(WebDriverException) as raised:
            document_api_service.create_web_document(kb.id, "owner", "page", "https://example.test/page")
        assert raised.value is error
        upload.assert_not_called()
    else:
        result = document_api_service.create_web_document(kb.id, "owner", "page", "https://example.test/page")
        assert result["id"] == "doc-pdf"
        assert result["dataset_id"] == kb.id
        upload.assert_called_once_with(db, kb, [(PDF, "page.pdf")], "owner")

    driver.quit.assert_called_once_with()
