from __future__ import annotations

import logging
import os
from collections.abc import Generator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from moodle import Moodle as MoodleClient
from moodle import MoodleException

from common.data_source.config import INDEX_BATCH_SIZE
from common.data_source.exceptions import (
    ConnectorMissingCredentialError,
    ConnectorValidationError,
    CredentialExpiredError,
    InsufficientPermissionsError,
)
from common.data_source.interfaces import (
    LoadConnector,
    PollConnector,
    SecondsSinceUnixEpoch,
    SlimConnectorWithPermSync,
)
from common.data_source.models import Document, SlimDocument
from common.data_source.utils import batch_generator, rl_requests
from common.markdown_utils import html_to_markdown as md

logger = logging.getLogger(__name__)


class MoodleConnector(LoadConnector, PollConnector, SlimConnectorWithPermSync):
    """Moodle LMS connector for accessing course content"""

    def __init__(self, moodle_url: str, batch_size: int = INDEX_BATCH_SIZE) -> None:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ConnectorValidationError("Moodle batch_size must be a positive integer")
        self._forum_cache: tuple[int, list[Any]] | None = None
        self.moodle_url = moodle_url.rstrip("/")
        self.batch_size = batch_size
        self.moodle_client: MoodleClient | None = None

    def _add_token_to_url(self, file_url: str) -> str:
        parts = urlsplit(file_url)
        origin = urlsplit(self.moodle_url)
        if parts.scheme not in {"http", "https"} or (parts.scheme, parts.netloc) != (origin.scheme, origin.netloc):
            raise ConnectorValidationError("Moodle file URL must belong to the configured server")
        query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key.lower() != "token"]
        query.append(("token", str(getattr(self.moodle_client, "token", ""))))
        return urlunsplit(parts._replace(query=urlencode(query)))

    def _log_error(self, context: str, error: Exception, level: str = "warning") -> None:
        # Provider exceptions can contain a download URL and its credential.
        logger.log(logging.ERROR if level == "error" else logging.WARNING, "%s (%s)", context, type(error).__name__)

    def _get_latest_timestamp(self, *timestamps: int | None) -> int:
        return max((value for value in timestamps if isinstance(value, int) and not isinstance(value, bool) and value > 0), default=0)

    def _api(self, function: str, **params: Any) -> Any:
        if self.moodle_client is None:
            raise ConnectorMissingCredentialError("Moodle client not initialized")
        try:
            result = self.moodle_client.post(function, **params)
        except Exception:
            raise ConnectorValidationError(f"Moodle {function} request failed") from None
        if isinstance(result, dict) and (result.get("exception") or result.get("errorcode")):
            raise ConnectorValidationError(f"Moodle {function} returned an API error")
        return result

    @staticmethod
    def _objects(value: Any, context: str) -> list[Any]:
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ConnectorValidationError(f"Moodle {context} must be a complete list")

        def convert(item: Any) -> Any:
            if isinstance(item, dict):
                return SimpleNamespace(**{key: convert(child) for key, child in item.items()})
            if isinstance(item, list):
                return [convert(child) for child in item]
            return item

        return [convert(item) for item in value]

    @staticmethod
    def _positive_id(value: Any) -> int:
        if isinstance(value, str) and value.isdecimal():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConnectorValidationError("Moodle returned an invalid identifier")
        return value

    def _module_id(self, module: Any) -> str:
        return f"moodle_{module.modname}_{self._positive_id(module.id)}"

    def _discussions(self, module: Any) -> list[Any]:
        key = self._positive_id(module.instance)
        if self._forum_cache is not None and self._forum_cache[0] == key:
            return self._forum_cache[1]
        result = self._api("mod_forum_get_forum_discussions", forumid=key, page=-1, perpage=0)
        if not isinstance(result, dict) or result.get("warnings") or "discussions" not in result:
            raise ConnectorValidationError("Moodle forum response is incomplete")
        discussions = self._objects(result["discussions"], "discussions")
        self._forum_cache = key, discussions
        return discussions

    def _eligible(self, module: Any) -> bool:
        if module.modname not in {"resource", "page", "forum", "assign", "quiz", "book"}:
            return False
        self._module_id(module)
        if getattr(module, "uservisible", True) is False:
            return False
        contents = getattr(module, "contents", [])
        if not isinstance(contents, list):
            raise ConnectorValidationError("Moodle module contents must be a list")
        for entry in contents:
            if not isinstance(entry, SimpleNamespace):
                raise ConnectorValidationError("Moodle file entry is malformed")
            if getattr(entry, "fileurl", None):
                if not isinstance(getattr(entry, "filename", None), str):
                    raise ConnectorValidationError("Moodle file name is missing")
                self._add_token_to_url(entry.fileurl)
        if module.modname in {"resource", "page"}:
            return bool(contents and getattr(contents[0], "fileurl", None) and getattr(contents[0], "filesize", None) != 0)
        if module.modname == "book":
            return any(getattr(entry, "fileurl", None) and os.path.basename(entry.filename) == "index.html" for entry in contents)
        if module.modname == "forum":
            return bool(getattr(module, "instance", None) and self._discussions(module))
        description = getattr(module, "description", "")
        if not isinstance(description, str):
            raise ConnectorValidationError("Moodle activity description is malformed")
        return bool(description.strip())

    def _iter_modules(self, courses: list[Any]) -> Generator[tuple[Any, Any, Any], None, None]:
        self._forum_cache = None
        seen: set[str] = set()
        for course in courses:
            for section in self._get_course_contents(self._positive_id(course.id)):
                modules = getattr(section, "modules", None)
                if not isinstance(modules, list):
                    raise ConnectorValidationError("Moodle section modules must be a complete list")
                for module in modules:
                    if not isinstance(module, SimpleNamespace) or not isinstance(getattr(module, "modname", None), str):
                        raise ConnectorValidationError("Moodle module is malformed")
                    if self._eligible(module):
                        identifier = self._module_id(module)
                        if identifier in seen:
                            raise ConnectorValidationError("Moodle returned a duplicate module identifier")
                        seen.add(identifier)
                        yield course, section, module

    def retrieve_all_slim_docs_perm_sync(self) -> Generator[list[SlimDocument], None, None]:
        batch: list[SlimDocument] = []
        for _, _, module in self._iter_modules(self._get_enrolled_courses()):
            batch.append(SlimDocument(id=self._module_id(module)))
            if len(batch) >= self.batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def _yield_in_batches(self, generator: Generator[Document, None, None]) -> Generator[list[Document], None, None]:
        yield from batch_generator(generator, self.batch_size)

    def load_credentials(self, credentials: dict[str, Any]) -> None:
        token = credentials.get("moodle_token")
        if not token:
            raise ConnectorMissingCredentialError("Moodle API token is required")

        try:
            self.moodle_client = MoodleClient(self.moodle_url + "/webservice/rest/server.php", token)
            self.moodle_client.core.webservice.get_site_info()
        except MoodleException as e:
            if "invalidtoken" in str(e).lower():
                raise CredentialExpiredError("Moodle token is invalid or expired")
            raise ConnectorMissingCredentialError("Failed to initialize Moodle client") from None

    def validate_connector_settings(self) -> None:
        if not self.moodle_client:
            raise ConnectorMissingCredentialError("Moodle client not initialized")

        try:
            site_info = self.moodle_client.core.webservice.get_site_info()
            if not site_info.sitename:
                raise InsufficientPermissionsError("Invalid Moodle API response")
        except MoodleException as e:
            msg = str(e).lower()
            if "invalidtoken" in msg:
                raise CredentialExpiredError("Moodle token is invalid or expired")
            if "accessexception" in msg:
                raise InsufficientPermissionsError("Insufficient permissions. Ensure web services are enabled and permissions are correct.")
            raise ConnectorValidationError("Moodle validation error") from None
        except Exception:
            raise ConnectorValidationError("Unexpected Moodle validation error") from None

    # -------------------------------------------------------------------------
    # Data loading & polling
    # -------------------------------------------------------------------------

    def load_from_state(self) -> Generator[list[Document], None, None]:
        if not self.moodle_client:
            raise ConnectorMissingCredentialError("Moodle client not initialized")

        logger.info("Starting full load from Moodle workspace")
        courses = self._get_enrolled_courses()
        if not courses:
            logger.warning("No courses found to process")
            return

        yield from self._yield_in_batches(self._process_courses(courses))

    def poll_source(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch) -> Generator[list[Document], None, None]:
        if not self.moodle_client:
            raise ConnectorMissingCredentialError("Moodle client not initialized")

        logger.info(f"Polling Moodle updates between {datetime.fromtimestamp(start)} and {datetime.fromtimestamp(end)}")
        courses = self._get_enrolled_courses()
        if not courses:
            logger.warning("No courses found to poll")
            return

        yield from self._yield_in_batches(self._get_updated_content(courses, start, end))

    def _get_enrolled_courses(self) -> list[Any]:
        return self._objects(self._api("core_course_get_courses"), "courses")

    def _get_course_contents(self, course_id: int) -> list[Any]:
        return self._objects(self._api("core_course_get_contents", courseid=course_id), "course contents")

    def _process_courses(self, courses: list[Any]) -> Generator[Document, None, None]:
        for course, section, module in self._iter_modules(courses):
            document = self._process_module(course, section, module)
            if document is not None:
                yield document

    def _get_updated_content(self, courses: list[Any], start: float, end: float) -> Generator[Document, None, None]:
        for course, section, module in self._iter_modules(courses):
            times = [getattr(module, "timecreated", 0), getattr(module, "timemodified", 0), getattr(module, "added", 0)]
            times.extend(getattr(entry, "timemodified", 0) for entry in getattr(module, "contents", []))
            if module.modname == "forum":
                times.extend(getattr(discussion, "timemodified", 0) for discussion in self._discussions(module))
            last_modified = self._get_latest_timestamp(*times)
            if last_modified == 0 or start < last_modified <= end:
                document = self._process_module(course, section, module)
                if document is not None:
                    yield document

    def _process_module(self, course: Any, section: Any, module: Any) -> Document | None:
        try:
            if module.modname == "resource":
                return self._process_resource(course, section, module)
            if module.modname == "forum":
                return self._process_forum(course, section, module)
            if module.modname == "page":
                return self._process_page(course, section, module)
            if module.modname in {"assign", "quiz"}:
                return self._process_activity(course, section, module)
            if module.modname == "book":
                return self._process_book(course, section, module)
        except Exception:
            raise ConnectorValidationError("Moodle module read failed") from None
        return None

    def _process_resource(self, course: Any, section: Any, module: Any) -> Document | None:
        if not getattr(module, "contents", None):
            return None

        file_info = module.contents[0]
        if not getattr(file_info, "fileurl", None):
            return None

        file_name = os.path.basename(file_info.filename)
        ts = self._get_latest_timestamp(
            getattr(module, "timecreated", 0),
            getattr(module, "timemodified", 0),
            getattr(file_info, "timemodified", 0),
        )

        try:
            resp = rl_requests.get(self._add_token_to_url(file_info.fileurl), timeout=60)
            resp.raise_for_status()
            blob = resp.content
            if not blob:
                raise ConnectorValidationError("Moodle returned an unexpectedly empty file")
            ext = os.path.splitext(file_name)[1] or ".bin"
            semantic_id = f"{course.fullname} / {section.name} / {file_name}"

            # Create metadata dictionary with relevant information
            metadata = {
                "moodle_url": self.moodle_url,
                "course_id": getattr(course, "id", None),
                "course_name": getattr(course, "fullname", None),
                "course_shortname": getattr(course, "shortname", None),
                "section_id": getattr(section, "id", None),
                "section_name": getattr(section, "name", None),
                "section_number": getattr(section, "section", None),
                "module_id": getattr(module, "id", None),
                "module_name": getattr(module, "name", None),
                "module_type": getattr(module, "modname", None),
                "module_instance": getattr(module, "instance", None),
                "file_url": getattr(file_info, "fileurl", None),
                "file_name": file_name,
                "file_size": getattr(file_info, "filesize", len(blob)),
                "file_type": getattr(file_info, "mimetype", None),
                "time_created": getattr(module, "timecreated", None),
                "time_modified": getattr(module, "timemodified", None),
                "visible": getattr(module, "visible", None),
                "groupmode": getattr(module, "groupmode", None),
            }

            return Document(
                id=self._module_id(module),
                source="moodle",
                semantic_identifier=semantic_id,
                extension=ext,
                blob=blob,
                doc_updated_at=datetime.fromtimestamp(ts or 0, tz=UTC),
                size_bytes=len(blob),
                metadata=metadata,
            )
        except Exception as e:
            self._log_error(f"downloading resource {file_name}", e, "error")
            raise ConnectorValidationError("Moodle resource download failed") from None

    def _process_forum(self, course: Any, section: Any, module: Any) -> Document | None:
        if not self.moodle_client or not getattr(module, "instance", None):
            return None

        try:
            disc_list = self._discussions(module)
            if not disc_list:
                return None

            markdown = [f"# {module.name}\n"]
            latest_ts = self._get_latest_timestamp(
                getattr(module, "timecreated", 0),
                getattr(module, "timemodified", 0),
            )

            for d in disc_list:
                markdown.append(f"## {d.name}\n\n{md(d.message or '')}\n\n---\n")
                latest_ts = max(latest_ts, getattr(d, "timemodified", 0))

            blob = "\n".join(markdown).encode("utf-8")
            semantic_id = f"{course.fullname} / {section.name} / {module.name}"

            # Create metadata dictionary with relevant information
            metadata = {
                "moodle_url": self.moodle_url,
                "course_id": getattr(course, "id", None),
                "course_name": getattr(course, "fullname", None),
                "course_shortname": getattr(course, "shortname", None),
                "section_id": getattr(section, "id", None),
                "section_name": getattr(section, "name", None),
                "section_number": getattr(section, "section", None),
                "module_id": getattr(module, "id", None),
                "module_name": getattr(module, "name", None),
                "module_type": getattr(module, "modname", None),
                "forum_id": getattr(module, "instance", None),
                "discussion_count": len(disc_list),
                "time_created": getattr(module, "timecreated", None),
                "time_modified": getattr(module, "timemodified", None),
                "visible": getattr(module, "visible", None),
                "groupmode": getattr(module, "groupmode", None),
                "discussions": [
                    {
                        "id": getattr(d, "id", None),
                        "name": getattr(d, "name", None),
                        "user_id": getattr(d, "userid", None),
                        "user_fullname": getattr(d, "userfullname", None),
                        "time_created": getattr(d, "timecreated", None),
                        "time_modified": getattr(d, "timemodified", None),
                    }
                    for d in disc_list
                ],
            }

            return Document(
                id=self._module_id(module),
                source="moodle",
                semantic_identifier=semantic_id,
                extension=".md",
                blob=blob,
                doc_updated_at=datetime.fromtimestamp(latest_ts or 0, tz=UTC),
                size_bytes=len(blob),
                metadata=metadata,
            )
        except Exception as e:
            self._log_error(f"processing forum {module.name}", e)
            raise ConnectorValidationError("Moodle forum read failed") from None

    def _process_page(self, course: Any, section: Any, module: Any) -> Document | None:
        if not getattr(module, "contents", None):
            return None

        file_info = module.contents[0]
        if not getattr(file_info, "fileurl", None):
            return None

        file_name = os.path.basename(file_info.filename)
        ts = self._get_latest_timestamp(
            getattr(module, "timecreated", 0),
            getattr(module, "timemodified", 0),
            getattr(file_info, "timemodified", 0),
        )

        try:
            resp = rl_requests.get(self._add_token_to_url(file_info.fileurl), timeout=60)
            resp.raise_for_status()
            blob = resp.content
            if not blob:
                raise ConnectorValidationError("Moodle returned an unexpectedly empty file")
            ext = os.path.splitext(file_name)[1] or ".html"
            semantic_id = f"{course.fullname} / {section.name} / {module.name}"

            # Create metadata dictionary with relevant information
            metadata = {
                "moodle_url": self.moodle_url,
                "course_id": getattr(course, "id", None),
                "course_name": getattr(course, "fullname", None),
                "course_shortname": getattr(course, "shortname", None),
                "section_id": getattr(section, "id", None),
                "section_name": getattr(section, "name", None),
                "section_number": getattr(section, "section", None),
                "module_id": getattr(module, "id", None),
                "module_name": getattr(module, "name", None),
                "module_type": getattr(module, "modname", None),
                "module_instance": getattr(module, "instance", None),
                "page_url": getattr(file_info, "fileurl", None),
                "file_name": file_name,
                "file_size": getattr(file_info, "filesize", len(blob)),
                "file_type": getattr(file_info, "mimetype", None),
                "time_created": getattr(module, "timecreated", None),
                "time_modified": getattr(module, "timemodified", None),
                "visible": getattr(module, "visible", None),
                "groupmode": getattr(module, "groupmode", None),
            }

            return Document(
                id=self._module_id(module),
                source="moodle",
                semantic_identifier=semantic_id,
                extension=ext,
                blob=blob,
                doc_updated_at=datetime.fromtimestamp(ts or 0, tz=UTC),
                size_bytes=len(blob),
                metadata=metadata,
            )
        except Exception as e:
            self._log_error(f"processing page {file_name}", e, "error")
            raise ConnectorValidationError("Moodle page download failed") from None

    def _process_activity(self, course: Any, section: Any, module: Any) -> Document | None:
        desc = getattr(module, "description", "")
        if not desc:
            return None

        mtype, mname = module.modname, module.name
        markdown = f"# {mname}\n\n**Type:** {mtype.capitalize()}\n\n{md(desc)}"
        ts = self._get_latest_timestamp(
            getattr(module, "timecreated", 0),
            getattr(module, "timemodified", 0),
            getattr(module, "added", 0),
        )

        semantic_id = f"{course.fullname} / {section.name} / {mname}"
        blob = markdown.encode("utf-8")

        # Create metadata dictionary with relevant information
        metadata = {
            "moodle_url": self.moodle_url,
            "course_id": getattr(course, "id", None),
            "course_name": getattr(course, "fullname", None),
            "course_shortname": getattr(course, "shortname", None),
            "section_id": getattr(section, "id", None),
            "section_name": getattr(section, "name", None),
            "section_number": getattr(section, "section", None),
            "module_id": getattr(module, "id", None),
            "module_name": getattr(module, "name", None),
            "module_type": getattr(module, "modname", None),
            "activity_type": mtype,
            "activity_instance": getattr(module, "instance", None),
            "description": desc,
            "time_created": getattr(module, "timecreated", None),
            "time_modified": getattr(module, "timemodified", None),
            "added": getattr(module, "added", None),
            "visible": getattr(module, "visible", None),
            "groupmode": getattr(module, "groupmode", None),
        }

        return Document(
            id=self._module_id(module),
            source="moodle",
            semantic_identifier=semantic_id,
            extension=".md",
            blob=blob,
            doc_updated_at=datetime.fromtimestamp(ts or 0, tz=UTC),
            size_bytes=len(blob),
            metadata=metadata,
        )

    def _process_book(self, course: Any, section: Any, module: Any) -> Document | None:
        if not getattr(module, "contents", None):
            return None

        contents = module.contents
        chapters = [c for c in contents if getattr(c, "fileurl", None) and os.path.basename(c.filename) == "index.html"]
        if not chapters:
            return None

        latest_ts = self._get_latest_timestamp(
            getattr(module, "timecreated", 0),
            getattr(module, "timemodified", 0),
            *[getattr(c, "timecreated", 0) for c in contents],
            *[getattr(c, "timemodified", 0) for c in contents],
        )

        markdown_parts = [f"# {module.name}\n"]
        chapter_info = []

        for ch in chapters:
            try:
                resp = rl_requests.get(self._add_token_to_url(ch.fileurl), timeout=60)
                resp.raise_for_status()
                html = resp.content.decode("utf-8", errors="ignore")
                markdown_parts.append(md(html) + "\n\n---\n")

                # Collect chapter information for metadata
                chapter_info.append(
                    {
                        "chapter_id": getattr(ch, "chapterid", None),
                        "title": getattr(ch, "title", None),
                        "filename": getattr(ch, "filename", None),
                        "fileurl": getattr(ch, "fileurl", None),
                        "time_created": getattr(ch, "timecreated", None),
                        "time_modified": getattr(ch, "timemodified", None),
                        "size": getattr(ch, "filesize", None),
                    }
                )
            except Exception as e:
                self._log_error("processing book chapter", e)
                raise ConnectorValidationError("Moodle book chapter download failed") from None

        blob = "\n".join(markdown_parts).encode("utf-8")
        semantic_id = f"{course.fullname} / {section.name} / {module.name}"

        # Create metadata dictionary with relevant information
        metadata = {
            "moodle_url": self.moodle_url,
            "course_id": getattr(course, "id", None),
            "course_name": getattr(course, "fullname", None),
            "course_shortname": getattr(course, "shortname", None),
            "section_id": getattr(section, "id", None),
            "section_name": getattr(section, "name", None),
            "section_number": getattr(section, "section", None),
            "module_id": getattr(module, "id", None),
            "module_name": getattr(module, "name", None),
            "module_type": getattr(module, "modname", None),
            "book_id": getattr(module, "instance", None),
            "chapter_count": len(chapters),
            "chapters": chapter_info,
            "time_created": getattr(module, "timecreated", None),
            "time_modified": getattr(module, "timemodified", None),
            "visible": getattr(module, "visible", None),
            "groupmode": getattr(module, "groupmode", None),
        }

        return Document(
            id=self._module_id(module),
            source="moodle",
            semantic_identifier=semantic_id,
            extension=".md",
            blob=blob,
            doc_updated_at=datetime.fromtimestamp(latest_ts or 0, tz=UTC),
            size_bytes=len(blob),
            metadata=metadata,
        )
