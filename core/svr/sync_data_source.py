import time

start_ts = time.time()

import asyncio
import copy
import faulthandler
import json
import logging
import math
import os
import signal
import sys
import threading
import traceback
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, TypeAlias

from box_sdk_gen import AccessToken, BoxOAuth, OAuthConfig

from api.db.db_models import db_connection
from api.db.services.connector_service import ConnectorService, SyncLogsService, SyncTaskCancelled, resolve_connector_doc_id
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from common import settings
from common.config_utils import show_configs
from common.constants import FileSource
from common.data_source import (
    AirtableConnector,
    AsanaConnector,
    BlobStorageConnector,
    DingTalkAITableConnector,
    DiscordConnector,
    DropboxConnector,
    GoogleDriveConnector,
    ImapConnector,
    JiraConnector,
    MoodleConnector,
    NotionConnector,
    RDBMSConnector,
    RestAPIConnector,
    RSSConnector,
    SeaFileConnector,
    ZendeskConnector,
)
from common.data_source.bitbucket.connector import BitbucketConnector
from common.data_source.box_connector import BoxConnector
from common.data_source.config import INDEX_BATCH_SIZE
from common.data_source.confluence_connector import ConfluenceConnector
from common.data_source.github.connector import GithubConnector
from common.data_source.gitlab_connector import GitlabConnector
from common.data_source.gmail_connector import GmailConnector
from common.data_source.imap_connector import _as_utc, imap_scope_signature
from common.data_source.interfaces import CheckpointOutputWrapper, GenerateDocumentsOutput, collect_slim_document_snapshot
from common.data_source.models import ConnectorFailure, Document, KeyRecord, SeafileSyncScope, SlimDocument
from common.data_source.rdbms_connector import rdbms_scope_signature
from common.data_source.webdav_connector import WebDAVConnector
from common.log_utils import init_root_logger
from common.signal_utils import start_tracemalloc_and_snapshot, stop_tracemalloc
from common.versions import get_multirag_version

GenerateDocumentsResult: TypeAlias = GenerateDocumentsOutput | tuple[GenerateDocumentsOutput, tuple[SlimDocument, ...] | None]

MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "5"))
task_limiter = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

DELETED_FILE_SYNC_SOURCES = frozenset(
    {
        FileSource.MOODLE,
        FileSource.MYSQL,
        FileSource.POSTGRESQL,
        FileSource.IMAP,
        FileSource.DINGTALK_AI_TABLE,
        FileSource.RSS,
        FileSource.WEBDAV,
        FileSource.AIRTABLE,
        FileSource.GOOGLE_DRIVE,
        FileSource.GMAIL,
        FileSource.BITBUCKET,
        FileSource.S3,
        FileSource.R2,
        FileSource.OCI_STORAGE,
        FileSource.GOOGLE_CLOUD_STORAGE,
        FileSource.CONFLUENCE,
        FileSource.NOTION,
        FileSource.JIRA,
        FileSource.BOX,
        FileSource.GITHUB,
        FileSource.GITLAB,
        FileSource.DROPBOX,
        FileSource.SEAFILE,
        FileSource.ASANA,
        FileSource.ZENDESK,
    }
)


class SyncBase:
    SOURCE_NAME: str = None

    def __init__(self, conf: dict) -> None:
        self.conf = conf

    async def __call__(self, task: dict) -> None:
        try:
            with db_connection() as db:
                SyncLogsService.start(db, task["id"], task["connector_id"])

            async with task_limiter:
                checkpoint = await asyncio.wait_for(self._run_task_logic(task), timeout=task["timeout_secs"])

            with db_connection() as db:
                completion = {"rdbms_state": self._completion_state()} if isinstance(self, _RDBMSBase) else {}
                SyncLogsService.complete_and_schedule_next(
                    db,
                    task["id"],
                    task["connector_id"],
                    task["kb_id"],
                    checkpoint,
                    **completion,
                )
        except SyncTaskCancelled:
            logging.info("Sync task remains cancelled: task_id=%s", task["id"])
        except TimeoutError:
            self._mark_failed(task, f"Task timeout after {task['timeout_secs']} seconds")
        except Exception as ex:
            trace = "\n".join(
                [
                    "".join(traceback.format_exception_only(None, ex)).strip(),
                    "".join(traceback.format_exception(None, ex, ex.__traceback__)).strip(),
                ]
            )
            self._mark_failed(task, str(ex), trace)

    @staticmethod
    def _mark_failed(task: dict, error_msg: str, full_exception_trace: str = "") -> None:
        try:
            with db_connection() as db:
                SyncLogsService.fail(
                    db,
                    task["id"],
                    task["connector_id"],
                    error_msg,
                    full_exception_trace,
                    task.get("poll_range_start"),
                )
        except Exception:
            logging.exception("Failed to persist sync task failure: task_id=%s", task.get("id"))

    async def _run_task_logic(self, task: dict) -> datetime:
        generate_output = await self._generate(task)
        if isinstance(generate_output, tuple):
            document_batch_generator, file_list = generate_output
        else:
            document_batch_generator = generate_output
            file_list = None

        if file_list is None and self.SOURCE_NAME in DELETED_FILE_SYNC_SOURCES and self.conf.get("sync_deleted_files") and task.get("reindex") != "1" and task.get("poll_range_start"):
            # Listing can block on remote pagination. Cancellation of this await
            # never hands a partially collected snapshot to the deletion service.
            file_list = await asyncio.to_thread(collect_slim_document_snapshot, self.connector)

        with db_connection() as db:
            SyncLogsService.raise_if_cancelled(db, task["id"])
            existing_doc_ids = {doc["id"] for doc in DocumentService.list_doc_headers_by_kb_and_source_type(db, task["kb_id"], f"{self.SOURCE_NAME}/{task['connector_id']}")}

        doc_num = 0
        failed_docs = 0
        removed_docs = 0
        ingested_source_ids: set[str] = set()
        next_update = datetime(1970, 1, 1, tzinfo=UTC)

        if task["poll_range_start"]:
            next_update = task["poll_range_start"]

        for document_batch in document_batch_generator:
            if not document_batch:
                continue

            max_update = max(doc.doc_updated_at for doc in document_batch)
            next_update = max(next_update, max_update)

            docs = []
            for doc in document_batch:
                d = {
                    "id": resolve_connector_doc_id(task["kb_id"], task["connector_id"], doc.id, existing_doc_ids),
                    "connector_id": task["connector_id"],
                    "source": self.SOURCE_NAME,
                    "semantic_identifier": doc.semantic_identifier,
                    "extension": doc.extension,
                    "size_bytes": doc.size_bytes,
                    "doc_updated_at": doc.doc_updated_at,
                    "blob": doc.blob,
                }
                if doc.metadata:
                    d["metadata"] = doc.metadata
                if getattr(doc, "fingerprint", None):
                    d["fingerprint"] = doc.fingerprint
                docs.append(d)

            try:
                with db_connection() as db:
                    SyncLogsService.raise_if_cancelled(db, task["id"])
                    kb = KnowledgebaseService.get_by_id(db, task["kb_id"])
                    err, dids = SyncLogsService.duplicate_and_parse(db, kb, docs, task["tenant_id"], f"{self.SOURCE_NAME}/{task['connector_id']}", task["auto_parse"])
                    SyncLogsService.increase_docs(db, task["id"], max_update, len(docs), "\n".join(err), len(err))

                doc_num += len(docs)
                failed_docs += len(err)
                existing_doc_ids.update(dids)
                ingested_source_ids.update(doc.id for doc in document_batch)

            except SyncTaskCancelled:
                raise
            except Exception as batch_ex:
                msg = str(batch_ex)
                code = getattr(batch_ex, "args", [None])[0]

                if code == 1267 or "collation" in msg.lower():
                    logging.warning(f"Skipping {len(docs)} document(s) due to collation conflict")
                else:
                    logging.error(f"Error processing batch: {msg}")

                failed_docs += len(docs)
                continue

        prefix = self._get_source_prefix()
        if failed_docs > 0:
            raise RuntimeError(f"{failed_docs} document(s) failed during synchronization; checkpoint was not committed")
        if file_list is not None:
            # An object can appear after enumeration and before incremental
            # ingestion. Never prune a source object just imported this run.
            file_list = (*file_list, *(SlimDocument(id=source_id) for source_id in ingested_source_ids))
            with db_connection() as db:
                removed_docs, cleanup_errors = ConnectorService.cleanup_stale_documents_for_task(
                    db,
                    task["id"],
                    task["connector_id"],
                    task["kb_id"],
                    task["tenant_id"],
                    file_list,
                )
            if cleanup_errors:
                raise RuntimeError("Deleted-file reconciliation failed; retry to reconcile: " + "; ".join(cleanup_errors))

        removed_info = f", {removed_docs} deleted" if file_list is not None else ""
        logging.info(f"{prefix}{doc_num} docs synchronized till {next_update}{removed_info}")
        return next_update

    async def _generate(self, task: dict[str, Any]) -> GenerateDocumentsResult:
        raise NotImplementedError

    def _get_source_prefix(self):
        return ""


class _BlobLikeBase(SyncBase):
    DEFAULT_BUCKET_TYPE: str = "s3"

    def _fingerprint_filtered_generator(self, task: dict[str, Any], records: list[KeyRecord]) -> GenerateDocumentsOutput:
        with db_connection() as db:
            existing = DocumentService.list_id_content_hash_map_by_kb_and_source_type(db, task["kb_id"], f"{self.SOURCE_NAME}/{task['connector_id']}")
        batch: list[Document] = []
        bypassed = fetched = excluded = 0
        for record in records:
            if record.deleted:
                continue
            doc_id = resolve_connector_doc_id(task["kb_id"], task["connector_id"], record.key, existing)
            if task["reindex"] != "1" and record.fingerprint and existing.get(doc_id) == record.fingerprint:
                bypassed += 1
                continue
            # Propagate errors: a partial fetch must not prune or report DONE.
            doc = self.connector.get_value(record.key)
            if doc is None:
                excluded += 1
                continue
            if doc.id != record.key:
                raise ValueError("Fetched blob identity differs from listed key")
            fetched += 1
            batch.append(doc)
            if len(batch) >= self.connector.batch_size:
                yield batch
                batch = []
        if batch:
            yield batch
        logging.info("[%s] fingerprint sync: %d bypassed, %d fetched, %d excluded (connector_id=%s, kb_id=%s)", self.SOURCE_NAME, bypassed, fetched, excluded, task["connector_id"], task["kb_id"])

    async def _generate(self, task: dict[str, Any]) -> tuple[GenerateDocumentsOutput, tuple[SlimDocument, ...] | None]:
        bucket_type = self.conf.get("bucket_type", self.DEFAULT_BUCKET_TYPE)
        self.connector = BlobStorageConnector(bucket_type=bucket_type, bucket_name=self.conf["bucket_name"], prefix=self.conf.get("prefix", ""))
        self.connector.set_allow_images(self.conf.get("allow_images", False))
        self.connector.load_credentials(self.conf["credentials"])
        records = await asyncio.to_thread(lambda: list(self.connector.list_keys()))
        # The same complete inventory retains bypassed objects, large objects
        # and fetched objects. No second, potentially different listing is used.
        file_list = None
        if self.conf.get("sync_deleted_files") and task["reindex"] != "1" and task.get("poll_range_start"):
            file_list = tuple(SlimDocument(id=record.key) for record in records if not record.deleted)
        logging.info("Connect to %s: %s(prefix/%s) fingerprint sync", bucket_type, self.conf["bucket_name"], self.conf.get("prefix", ""))
        return self._fingerprint_filtered_generator(task, records), file_list


class S3(_BlobLikeBase):
    SOURCE_NAME: str = FileSource.S3
    DEFAULT_BUCKET_TYPE: str = "s3"


class R2(_BlobLikeBase):
    SOURCE_NAME: str = FileSource.R2
    DEFAULT_BUCKET_TYPE: str = "r2"


class OCI_STORAGE(_BlobLikeBase):
    SOURCE_NAME: str = FileSource.OCI_STORAGE
    DEFAULT_BUCKET_TYPE: str = "oci_storage"


class GOOGLE_CLOUD_STORAGE(_BlobLikeBase):
    SOURCE_NAME: str = FileSource.GOOGLE_CLOUD_STORAGE
    DEFAULT_BUCKET_TYPE: str = "google_cloud_storage"


class RSS(SyncBase):
    SOURCE_NAME: str = FileSource.RSS

    async def _generate(self, task: dict[str, Any]) -> GenerateDocumentsOutput:
        self.connector = RSSConnector(
            feed_url=self.conf["feed_url"],
            batch_size=self.conf.get("batch_size", INDEX_BATCH_SIZE),
        )
        self.connector.load_credentials(self.conf.get("credentials", {}))
        self.connector.validate_connector_settings()

        if task["reindex"] == "1" or not task["poll_range_start"]:
            return self.connector.load_from_state()

        return self.connector.poll_source(
            task["poll_range_start"].timestamp(),
            datetime.now(UTC).timestamp(),
        )


class Confluence(SyncBase):
    SOURCE_NAME: str = FileSource.CONFLUENCE

    async def _generate(self, task: dict):
        from common.data_source.config import DocumentSource
        from common.data_source.interfaces import StaticCredentialsProvider

        index_mode = (self.conf.get("index_mode") or "everything").lower()
        if index_mode not in {"everything", "space", "page"}:
            index_mode = "everything"

        space = ""
        page_id = ""

        index_recursively = False
        if index_mode == "space":
            space = (self.conf.get("space") or "").strip()
            if not space:
                raise ValueError("Space Key is required when indexing a specific Confluence space.")
        elif index_mode == "page":
            page_id = (self.conf.get("page_id") or "").strip()
            if not page_id:
                raise ValueError("Page ID is required when indexing a specific Confluence page.")
            index_recursively = bool(self.conf.get("index_recursively", False))

        self.connector = ConfluenceConnector(
            wiki_base=self.conf["wiki_base"],
            is_cloud=self.conf.get("is_cloud", True),
            space=space,
            page_id=page_id,
            index_recursively=index_recursively,
        )

        credentials_provider = StaticCredentialsProvider(tenant_id=task["tenant_id"], connector_name=DocumentSource.CONFLUENCE, credential_json=self.conf["credentials"])
        self.connector.set_credentials_provider(credentials_provider)

        # Determine the time range for synchronization based on reindex or poll_range_start
        if task["reindex"] == "1" or not task["poll_range_start"]:
            start_time = 0.0
            _begin_info = "totally"
        else:
            start_time = task["poll_range_start"].timestamp()
            _begin_info = f"from {task['poll_range_start']}"

        end_time = datetime.now(UTC).timestamp()

        raw_batch_size = self.conf.get("sync_batch_size") or self.conf.get("batch_size") or INDEX_BATCH_SIZE
        try:
            batch_size = int(raw_batch_size)
        except (TypeError, ValueError):
            batch_size = INDEX_BATCH_SIZE
        if batch_size <= 0:
            batch_size = INDEX_BATCH_SIZE

        def document_batches():
            checkpoint = self.connector.build_dummy_checkpoint()
            pending_docs = []
            iterations = 0
            iteration_limit = 100_000

            while checkpoint.has_more:
                wrapper = CheckpointOutputWrapper()
                doc_generator = wrapper(self.connector.load_from_checkpoint(start_time, end_time, checkpoint))
                for document, failure, next_checkpoint in doc_generator:
                    if failure is not None:
                        raise RuntimeError(f"Confluence connector failed: {getattr(failure, 'failure_message', failure)}")
                    if document is not None:
                        pending_docs.append(document)
                        if len(pending_docs) >= batch_size:
                            yield pending_docs
                            pending_docs = []
                    if next_checkpoint is not None:
                        checkpoint = next_checkpoint

                iterations += 1
                if iterations > iteration_limit:
                    raise RuntimeError("Too many iterations while loading Confluence documents.")

            if pending_docs:
                yield pending_docs

        def wrapper():
            yield from document_batches()

        logging.info("Connect to Confluence: {} {}".format(self.conf["wiki_base"], _begin_info))
        return wrapper()


class IMAP(SyncBase):
    SOURCE_NAME: str = FileSource.IMAP

    async def _generate(self, task: dict[str, Any]) -> GenerateDocumentsOutput:
        from common.data_source.config import DocumentSource
        from common.data_source.interfaces import StaticCredentialsProvider

        end_time = datetime.now(UTC).timestamp()
        try:
            poll_range = float(self.conf.get("poll_range", 30))
        except (TypeError, ValueError):
            poll_range = 30.0
        if not math.isfinite(poll_range) or poll_range < 0:
            raise ValueError("IMAP poll_range must be finite and nonnegative")
        default_start = max(0.0, end_time - poll_range * 86400)
        poll_start = task.get("poll_range_start")
        reindex = task.get("reindex") == "1"

        def persist_scope() -> float:
            with db_connection() as db:
                return ConnectorService.ensure_imap_sync_scope(
                    db, task["connector_id"], task["kb_id"], task["id"], imap_scope_signature(self.conf), default_start, reindex=reindex, incremental=poll_start is not None
                )

        initial_start = await asyncio.to_thread(persist_scope)
        start_time = initial_start if reindex or poll_start is None else _as_utc(poll_start).timestamp()
        self.connector = ImapConnector(
            host=self.conf["imap_host"],
            port=int(self.conf.get("imap_port") or 993),
            mailboxes=self.conf.get("imap_mailbox"),
            snapshot_start=initial_start,
            snapshot_end=end_time,
        )
        credentials_provider = StaticCredentialsProvider(tenant_id=task["tenant_id"], connector_name=DocumentSource.IMAP, credential_json=self.conf["credentials"])
        self.connector.set_credentials_provider(credentials_provider)
        _begin_info = f"from {datetime.fromtimestamp(start_time, UTC)}"
        raw_batch_size = self.conf.get("sync_batch_size") or self.conf.get("batch_size") or INDEX_BATCH_SIZE
        try:
            batch_size = int(raw_batch_size)
        except (TypeError, ValueError):
            batch_size = INDEX_BATCH_SIZE
        if batch_size <= 0:
            batch_size = INDEX_BATCH_SIZE

        def document_batches() -> GenerateDocumentsOutput:
            checkpoint = self.connector.build_dummy_checkpoint()
            pending_docs = []
            iterations = 0
            iteration_limit = 100_000
            while checkpoint.has_more:
                wrapper = CheckpointOutputWrapper()
                doc_generator = wrapper(self.connector.load_from_checkpoint(start_time, end_time, checkpoint))
                for document, failure, next_checkpoint in doc_generator:
                    if failure is not None:
                        raise RuntimeError(f"IMAP connector failure: {getattr(failure, 'failure_message', failure)}")
                    if document is not None:
                        pending_docs.append(document)
                        if len(pending_docs) >= batch_size:
                            yield pending_docs
                            pending_docs = []
                    if next_checkpoint is not None:
                        checkpoint = next_checkpoint

                iterations += 1
                if iterations > iteration_limit:
                    raise RuntimeError("Too many iterations while loading IMAP documents.")

            if pending_docs:
                yield pending_docs

        def wrapper() -> GenerateDocumentsOutput:
            yield from document_batches()

        logging.info(
            "Connect to IMAP: host(%s) port(%s) user(%s) folder(%s) %s",
            self.conf["imap_host"],
            self.conf["imap_port"],
            self.conf["credentials"]["imap_username"],
            self.conf.get("imap_mailbox"),
            _begin_info,
        )
        return wrapper()


class Zendesk(SyncBase):
    SOURCE_NAME: str = FileSource.ZENDESK

    async def _generate(self, task: dict[str, Any]) -> GenerateDocumentsOutput:
        self.connector = ZendeskConnector(content_type=self.conf.get("zendesk_content_type", "articles"))
        self.connector.load_credentials(self.conf["credentials"])

        end_time = datetime.now(UTC).timestamp()
        if task["reindex"] == "1" or not task.get("poll_range_start"):
            start_time = 0
            _begin_info = "totally"
        else:
            start_time = task["poll_range_start"].timestamp()
            _begin_info = f"from {task['poll_range_start']}"

        raw_batch_size = self.conf.get("sync_batch_size") or self.conf.get("batch_size") or INDEX_BATCH_SIZE
        try:
            batch_size = int(raw_batch_size)
        except (TypeError, ValueError):
            batch_size = INDEX_BATCH_SIZE

        if batch_size <= 0:
            batch_size = INDEX_BATCH_SIZE

        def document_batches() -> GenerateDocumentsOutput:
            checkpoint = self.connector.build_dummy_checkpoint()
            pending_docs = []
            iterations = 0
            iteration_limit = 100_000

            while checkpoint.has_more:
                wrapper = CheckpointOutputWrapper()
                doc_generator = wrapper(self.connector.load_from_checkpoint(start_time, end_time, checkpoint))

                for document, failure, next_checkpoint in doc_generator:
                    if failure is not None:
                        raise RuntimeError(f"Zendesk content failed: {failure.failure_message}")

                    if document is not None:
                        pending_docs.append(document)
                        if len(pending_docs) >= batch_size:
                            yield pending_docs
                            pending_docs = []

                    if next_checkpoint is not None:
                        checkpoint = next_checkpoint

                iterations += 1
                if iterations > iteration_limit:
                    raise RuntimeError("Too many iterations while loading Zendesk documents.")

            if pending_docs:
                yield pending_docs

        def wrapper() -> GenerateDocumentsOutput:
            yield from document_batches()

        logging.info(
            "Connect to Zendesk: subdomain(%s) %s",
            self.conf["credentials"].get("zendesk_subdomain"),
            _begin_info,
        )

        return wrapper()


class Notion(SyncBase):
    SOURCE_NAME: str = FileSource.NOTION

    async def _generate(self, task: dict):
        self.connector = NotionConnector(root_page_id=self.conf["root_page_id"])
        self.connector.load_credentials(self.conf["credentials"])
        document_generator = (
            self.connector.load_from_state()
            if task["reindex"] == "1" or not task["poll_range_start"]
            else self.connector.poll_source(task["poll_range_start"].timestamp(), datetime.now(UTC).timestamp())
        )

        _begin_info = "totally" if task["reindex"] == "1" or not task["poll_range_start"] else "from {}".format(task["poll_range_start"])
        logging.info("Connect to Notion: root({}) {}".format(self.conf["root_page_id"], _begin_info))
        return document_generator


class Discord(SyncBase):
    SOURCE_NAME: str = FileSource.DISCORD

    @staticmethod
    def _coerce_str_list(raw: Any) -> list[str]:
        if raw is None:
            return []
        if isinstance(raw, str):
            items = raw.split(",")
        elif isinstance(raw, list | tuple | set):
            items = list(raw)
        else:
            raise ValueError("Discord scope must be a list or comma-separated string")
        cleaned: list[str] = []
        for item in items:
            if item is None:
                continue
            if isinstance(item, bool) or not isinstance(item, str | int):
                raise ValueError("Discord scope entries must be strings or integer IDs")
            if value := str(item).strip():
                cleaned.append(value)
        return cleaned

    async def _generate(self, task: dict[str, Any]) -> GenerateDocumentsOutput:
        server_ids = self._coerce_str_list(self.conf.get("server_ids"))
        channels = self.conf.get("channels")
        if channels in (None, "", []):
            channels = self.conf.get("channel_names")
        channel_names = self._coerce_str_list(channels)

        self.connector = DiscordConnector(
            # Invalid IDs must fail; dropping them could widen the configured scope.
            server_ids=server_ids,
            channel_names=channel_names,
            start_date=datetime(1970, 1, 1, tzinfo=UTC).strftime("%Y-%m-%d"),
            batch_size=self.conf.get("batch_size", 1024),
        )
        self.connector.load_credentials(self.conf["credentials"])
        document_generator = (
            self.connector.load_from_state()
            if task["reindex"] == "1" or not task["poll_range_start"]
            else self.connector.poll_source(task["poll_range_start"].timestamp(), datetime.now(UTC).timestamp())
        )

        _begin_info = "totally" if task["reindex"] == "1" or not task["poll_range_start"] else "from {}".format(task["poll_range_start"])
        logging.info(f"Connect to Discord: servers({server_ids}),  channel({channel_names}) {_begin_info}")
        return document_generator


class Gmail(SyncBase):
    SOURCE_NAME: str = FileSource.GMAIL

    async def _generate(self, task: dict[str, Any]) -> GenerateDocumentsOutput:
        # Gmail sync reuses the generic LoadConnector/PollConnector interface
        # implemented by common.data_source.gmail_connector.GmailConnector.
        #
        # Config expectations (self.conf):
        #   credentials: Gmail / Workspace OAuth JSON (with primary admin email)
        #   batch_size:  optional, defaults to INDEX_BATCH_SIZE
        batch_size = self.conf.get("batch_size", INDEX_BATCH_SIZE)

        self.connector = GmailConnector(batch_size=batch_size, require_complete=bool(self.conf.get("sync_deleted_files")))

        credentials = self.conf.get("credentials")
        if not credentials:
            raise ValueError("Gmail connector is missing credentials.")

        new_credentials = self.connector.load_credentials(credentials)
        if new_credentials:
            # Persist rotated / refreshed credentials back to connector config
            try:
                updated_conf = copy.deepcopy(self.conf)
                updated_conf["credentials"] = new_credentials
                with db_connection() as db:
                    ConnectorService.update_by_id(db, task["connector_id"], {"config": updated_conf})
                self.conf = updated_conf
                logging.info(
                    "Persisted refreshed Gmail credentials for connector %s",
                    task["connector_id"],
                )
            except Exception:
                logging.exception(
                    "Failed to persist refreshed Gmail credentials for connector %s",
                    task["connector_id"],
                )

        # Decide between full reindex and incremental polling by time range.
        if task["reindex"] == "1" or not task.get("poll_range_start"):
            start_time = None
            end_time = None
            _begin_info = "totally"
            document_generator = self.connector.load_from_state()
        else:
            poll_start = task["poll_range_start"]
            # Defensive: if poll_start is somehow None, fall back to full load
            if poll_start is None:
                start_time = None
                end_time = None
                _begin_info = "totally"
                document_generator = self.connector.load_from_state()
            else:
                start_time = poll_start.timestamp()
                end_time = datetime.now(UTC).timestamp()
                _begin_info = f"from {poll_start}"
                document_generator = self.connector.poll_source(start_time, end_time)

        try:
            admin_email = self.connector.primary_admin_email
        except RuntimeError:
            admin_email = "unknown"
        logging.info(f"Connect to Gmail as {admin_email} {_begin_info}")
        return document_generator


class Dropbox(SyncBase):
    SOURCE_NAME: str = FileSource.DROPBOX

    async def _generate(self, task: dict[str, Any]) -> GenerateDocumentsOutput:
        self.connector = DropboxConnector(batch_size=self.conf.get("batch_size", INDEX_BATCH_SIZE))
        self.connector.load_credentials(self.conf["credentials"])

        if task["reindex"] == "1" or not task["poll_range_start"]:
            document_generator = self.connector.load_from_state()
            _begin_info = "totally"
        else:
            poll_start = task["poll_range_start"]
            document_generator = self.connector.poll_source(poll_start.timestamp(), datetime.now(UTC).timestamp())
            _begin_info = f"from {poll_start}"

        logging.info(f"[Dropbox] Connect to Dropbox {_begin_info}")
        return document_generator


class GoogleDrive(SyncBase):
    SOURCE_NAME: str = FileSource.GOOGLE_DRIVE

    async def _generate(self, task: dict[str, Any]) -> GenerateDocumentsOutput:
        connector_kwargs = {
            "include_shared_drives": self.conf.get("include_shared_drives", False),
            "include_my_drives": self.conf.get("include_my_drives", False),
            "include_files_shared_with_me": self.conf.get("include_files_shared_with_me", False),
            "shared_drive_urls": self.conf.get("shared_drive_urls"),
            "my_drive_emails": self.conf.get("my_drive_emails"),
            "shared_folder_urls": self.conf.get("shared_folder_urls"),
            "specific_user_emails": self.conf.get("specific_user_emails"),
            "batch_size": self.conf.get("batch_size", INDEX_BATCH_SIZE),
        }
        self.connector = GoogleDriveConnector(**connector_kwargs)
        self.connector.set_allow_images(self.conf.get("allow_images", False))

        credentials = self.conf.get("credentials")
        if not credentials:
            raise ValueError("Google Drive connector is missing credentials.")

        new_credentials = self.connector.load_credentials(credentials)
        if new_credentials:
            self._persist_rotated_credentials(task["connector_id"], new_credentials)

        if task["reindex"] == "1" or not task["poll_range_start"]:
            start_time = 0.0
            _begin_info = "totally"
        else:
            start_time = task["poll_range_start"].timestamp()
            _begin_info = f"from {task['poll_range_start']}"

        end_time = datetime.now(UTC).timestamp()
        raw_batch_size = self.conf.get("sync_batch_size") or self.conf.get("batch_size") or INDEX_BATCH_SIZE
        try:
            batch_size = int(raw_batch_size)
        except (TypeError, ValueError):
            batch_size = INDEX_BATCH_SIZE
        if batch_size <= 0:
            batch_size = INDEX_BATCH_SIZE

        def document_batches() -> Iterator[list[Document]]:
            checkpoint = self.connector.build_dummy_checkpoint()
            pending_docs: list[Document] = []
            iterations = 0
            iteration_limit = 100_000

            while checkpoint.has_more:
                wrapper = CheckpointOutputWrapper()
                doc_generator = wrapper(self.connector.load_from_checkpoint(start_time, end_time, checkpoint))
                for document, failure, next_checkpoint in doc_generator:
                    if failure is not None:
                        raise RuntimeError(f"Google Drive ingestion failed: {failure.failure_message}")
                    if document is not None:
                        pending_docs.append(document)
                        if len(pending_docs) >= batch_size:
                            yield pending_docs
                            pending_docs = []
                    if next_checkpoint is not None:
                        checkpoint = next_checkpoint

                iterations += 1
                if iterations > iteration_limit:
                    raise RuntimeError("Too many iterations while loading Google Drive documents.")

            if pending_docs:
                yield pending_docs

        try:
            admin_email = self.connector.primary_admin_email
        except RuntimeError:
            admin_email = "unknown"
        logging.info(f"Connect to Google Drive as {admin_email} {_begin_info}")
        return document_batches()

    def _persist_rotated_credentials(self, connector_id: str, credentials: dict[str, Any]) -> None:
        try:
            updated_conf = copy.deepcopy(self.conf)
            updated_conf["credentials"] = credentials
            with db_connection() as db:
                ConnectorService.update_by_id(db, connector_id, {"config": updated_conf})
            self.conf = updated_conf
            logging.info("Persisted refreshed Google Drive credentials for connector %s", connector_id)
        except Exception:
            logging.exception("Failed to persist refreshed Google Drive credentials for connector %s", connector_id)


class Jira(SyncBase):
    SOURCE_NAME: str = FileSource.JIRA

    def _get_source_prefix(self):
        return "[Jira]"

    async def _generate(self, task: dict):
        connector_kwargs = {
            "jira_base_url": self.conf["base_url"],
            "project_key": self.conf.get("project_key"),
            "jql_query": self.conf.get("jql_query"),
            "batch_size": self.conf.get("batch_size", INDEX_BATCH_SIZE),
            "include_comments": self.conf.get("include_comments", True),
            "include_attachments": self.conf.get("include_attachments", False),
            "labels_to_skip": self._normalize_list(self.conf.get("labels_to_skip")),
            "comment_email_blacklist": self._normalize_list(self.conf.get("comment_email_blacklist")),
            "scoped_token": self.conf.get("scoped_token", False),
            "attachment_size_limit": self.conf.get("attachment_size_limit"),
            "timezone_offset": self.conf.get("timezone_offset"),
            "time_buffer_seconds": self.conf.get("time_buffer_seconds"),
        }

        self.connector = JiraConnector(**connector_kwargs)

        credentials = self.conf.get("credentials")
        if not credentials:
            raise ValueError("Jira connector is missing credentials.")

        self.connector.load_credentials(credentials)
        self.connector.validate_connector_settings()

        if task["reindex"] == "1" or not task["poll_range_start"]:
            start_time = 0.0
            _begin_info = "totally"
        else:
            start_time = task["poll_range_start"].timestamp()
            _begin_info = f"from {task['poll_range_start']}"

        end_time = datetime.now(UTC).timestamp()

        raw_batch_size = self.conf.get("sync_batch_size") or self.conf.get("batch_size") or INDEX_BATCH_SIZE
        try:
            batch_size = int(raw_batch_size)
        except (TypeError, ValueError):
            batch_size = INDEX_BATCH_SIZE
        if batch_size <= 0:
            batch_size = INDEX_BATCH_SIZE

        def document_batches():
            checkpoint = self.connector.build_dummy_checkpoint()
            pending_docs = []
            iterations = 0
            iteration_limit = 100_000

            while checkpoint.has_more:
                wrapper = CheckpointOutputWrapper()
                generator = wrapper(
                    self.connector.load_from_checkpoint(
                        start_time,
                        end_time,
                        checkpoint,
                    )
                )
                for document, failure, next_checkpoint in generator:
                    if failure is not None:
                        raise RuntimeError(f"Jira connector failed: {getattr(failure, 'failure_message', failure)}")
                    if document is not None:
                        pending_docs.append(document)
                        if len(pending_docs) >= batch_size:
                            yield pending_docs
                            pending_docs = []
                    if next_checkpoint is not None:
                        checkpoint = next_checkpoint

                iterations += 1
                if iterations > iteration_limit:
                    logging.error(f"[Jira] Task {task.get('id')} exceeded iteration limit ({iteration_limit}).")
                    raise RuntimeError("Too many iterations while loading Jira documents.")

            if pending_docs:
                yield pending_docs

        logging.info(
            "[Jira] Connect to Jira %s %s (start=%s, end=%s, sync_batch_size=%s, overlap_buffer_s=%s)",
            connector_kwargs["jira_base_url"],
            _begin_info,
            start_time,
            end_time,
            batch_size,
            getattr(self.connector, "time_buffer_seconds", connector_kwargs.get("time_buffer_seconds")),
        )
        return document_batches()

    @staticmethod
    def _normalize_list(values: Any) -> list[str] | None:
        if values is None:
            return None
        if isinstance(values, str):
            values = [item.strip() for item in values.split(",")]
        return [str(value).strip() for value in values if value is not None and str(value).strip()]


class SharePoint(SyncBase):
    SOURCE_NAME: str = FileSource.SHAREPOINT

    async def _generate(self, task: dict):
        pass


class Slack(SyncBase):
    SOURCE_NAME: str = FileSource.SLACK

    async def _generate(self, task: dict):
        pass


class Teams(SyncBase):
    SOURCE_NAME: str = FileSource.TEAMS

    async def _generate(self, task: dict):
        pass


class WebDAV(SyncBase):
    SOURCE_NAME: str = FileSource.WEBDAV

    async def _generate(self, task: dict[str, Any]) -> GenerateDocumentsOutput:
        try:
            batch_size = int(self.conf.get("batch_size", INDEX_BATCH_SIZE))
        except (TypeError, ValueError):
            batch_size = INDEX_BATCH_SIZE
        if batch_size <= 0:
            batch_size = INDEX_BATCH_SIZE
        self.connector = WebDAVConnector(base_url=self.conf["base_url"], remote_path=self.conf.get("remote_path", "/"), batch_size=batch_size)
        self.connector.set_allow_images(self.conf.get("allow_images", False))
        self.connector.load_credentials(self.conf["credentials"])

        logging.info(f"Task info: reindex={task['reindex']}, poll_range_start={task['poll_range_start']}")

        if task["reindex"] == "1" or not task["poll_range_start"]:
            logging.info("Using load_from_state (full sync)")
            document_batch_generator = self.connector.load_from_state()
            _begin_info = "totally"
        else:
            start_ts = task["poll_range_start"].timestamp()
            end_ts = datetime.now(UTC).timestamp()
            logging.info(f"Polling WebDAV from {task['poll_range_start']} (ts: {start_ts}) to now (ts: {end_ts})")
            document_batch_generator = self.connector.poll_source(start_ts, end_ts)
            _begin_info = "from {}".format(task["poll_range_start"])

        logging.info("Connect to WebDAV: {}(path: {}) {}".format(self.conf["base_url"], self.conf.get("remote_path", "/"), _begin_info))

        return document_batch_generator


class Moodle(SyncBase):
    SOURCE_NAME: str = FileSource.MOODLE

    async def _generate(self, task: dict[str, Any]) -> GenerateDocumentsOutput:
        self.connector = MoodleConnector(moodle_url=self.conf["moodle_url"], batch_size=self.conf.get("batch_size", INDEX_BATCH_SIZE))

        self.connector.load_credentials(self.conf["credentials"])

        # Determine the time range for synchronization based on reindex or poll_range_start
        poll_start = task.get("poll_range_start")

        if task["reindex"] == "1" or poll_start is None:
            document_generator = self.connector.load_from_state()
            _begin_info = "totally"
        else:
            document_generator = self.connector.poll_source(
                poll_start.timestamp(),
                datetime.now(UTC).timestamp(),
            )
            _begin_info = f"from {poll_start}"

        logging.info("Connect to Moodle: {} {}".format(self.conf["moodle_url"], _begin_info))
        return document_generator


class BOX(SyncBase):
    SOURCE_NAME: str = FileSource.BOX

    async def _generate(self, task: dict):
        self.connector = BoxConnector(
            folder_id=self.conf.get("folder_id", "0"),
        )

        credential = json.loads(self.conf["credentials"]["box_tokens"])

        auth = BoxOAuth(
            OAuthConfig(
                client_id=credential["client_id"],
                client_secret=credential["client_secret"],
            )
        )

        token = AccessToken(
            access_token=credential["access_token"],
            refresh_token=credential["refresh_token"],
        )
        auth.token_storage.store(token)

        self.connector.load_credentials(auth)
        poll_start = task["poll_range_start"]

        if task["reindex"] == "1" or poll_start is None:
            document_generator = self.connector.load_from_state()
            _begin_info = "totally"
        else:
            document_generator = self.connector.poll_source(
                poll_start.timestamp(),
                datetime.now(UTC).timestamp(),
            )
            _begin_info = f"from {poll_start}"
        logging.info("Connect to Box: folder_id({}) {}".format(self.conf["folder_id"], _begin_info))
        return document_generator


class Airtable(SyncBase):
    SOURCE_NAME: str = FileSource.AIRTABLE

    async def _generate(self, task: dict):
        """
        Sync files from Airtable attachments.
        """

        self.connector = AirtableConnector(
            base_id=self.conf.get("base_id"),
            table_name_or_id=self.conf.get("table_name_or_id"),
        )

        credentials = self.conf.get("credentials", {})
        if "airtable_access_token" not in credentials:
            raise ValueError("Missing airtable_access_token in credentials")

        self.connector.load_credentials({"airtable_access_token": credentials["airtable_access_token"]})

        poll_start = task.get("poll_range_start")

        if task.get("reindex") == "1" or poll_start is None:
            document_generator = self.connector.load_from_state()
            _begin_info = "totally"
        else:
            document_generator = self.connector.poll_source(
                poll_start.timestamp(),
                datetime.now(UTC).timestamp(),
            )
            _begin_info = f"from {poll_start}"

        logging.info(
            "Connect to Airtable: base_id(%s), table(%s) %s",
            self.conf.get("base_id"),
            self.conf.get("table_name_or_id"),
            _begin_info,
        )

        return document_generator


class Asana(SyncBase):
    SOURCE_NAME: str = FileSource.ASANA

    async def _generate(self, task: dict[str, Any]) -> GenerateDocumentsOutput:
        self.connector = AsanaConnector(
            self.conf.get("asana_workspace_id"),
            self.conf.get("asana_project_ids"),
            self.conf.get("asana_team_id"),
            batch_size=self.conf.get("batch_size", INDEX_BATCH_SIZE),
        )
        credentials = self.conf.get("credentials", {})
        if "asana_api_token_secret" not in credentials:
            raise ValueError("Missing asana_api_token_secret in credentials")

        self.connector.load_credentials({"asana_api_token_secret": credentials["asana_api_token_secret"]})

        if task.get("reindex") == "1" or not task.get("poll_range_start"):
            document_generator = self.connector.load_from_state()
            _begin_info = "totally"
        else:
            poll_start = task.get("poll_range_start")
            if poll_start is None:
                document_generator = self.connector.load_from_state()
                _begin_info = "totally"
            else:
                document_generator = self.connector.poll_source(
                    poll_start.timestamp(),
                    datetime.now(UTC).timestamp(),
                )
                _begin_info = f"from {poll_start}"

        logging.info(
            "Connect to Asana: workspace_id(%s), project_ids(%s), team_id(%s) %s",
            self.conf.get("asana_workspace_id"),
            self.conf.get("asana_project_ids"),
            self.conf.get("asana_team_id"),
            _begin_info,
        )

        return document_generator


class Github(SyncBase):
    SOURCE_NAME: str = FileSource.GITHUB

    async def _generate(self, task: dict):
        """
        Sync files from Github repositories.
        """
        from common.data_source.connector_runner import ConnectorRunner

        self.connector = GithubConnector(
            repo_owner=self.conf.get("repository_owner"),
            repositories=self.conf.get("repository_name"),
            include_prs=self.conf.get("include_pull_requests", False),
            include_issues=self.conf.get("include_issues", False),
        )

        credentials = self.conf.get("credentials", {})
        if "github_access_token" not in credentials:
            raise ValueError("Missing github_access_token in credentials")

        self.connector.load_credentials({"github_access_token": credentials["github_access_token"]})

        if task.get("reindex") == "1" or not task.get("poll_range_start"):
            start_time = datetime.fromtimestamp(0, tz=UTC)
            _begin_info = "totally"
        else:
            start_time = task.get("poll_range_start")
            _begin_info = f"from {start_time}"

        end_time = datetime.now(UTC)

        runner = ConnectorRunner(connector=self.connector, batch_size=self.conf.get("batch_size", INDEX_BATCH_SIZE), include_permissions=False, time_range=(start_time, end_time))

        def document_batches():
            checkpoint = self.connector.build_dummy_checkpoint()

            while checkpoint.has_more:
                for doc_batch, failure, next_checkpoint in runner.run(checkpoint):
                    if failure is not None:
                        raise RuntimeError(f"Github connector failed: {getattr(failure, 'failure_message', failure)}")
                    if doc_batch is not None:
                        yield doc_batch
                    if next_checkpoint is not None:
                        checkpoint = next_checkpoint

        def wrapper():
            yield from document_batches()

        logging.info(
            "Connect to Github: org_name(%s), repo_names(%s) for %s",
            self.conf.get("repository_owner"),
            self.conf.get("repository_name"),
            _begin_info,
        )

        return wrapper()


class Gitlab(SyncBase):
    SOURCE_NAME: str = FileSource.GITLAB

    async def _generate(self, task: dict[str, Any]) -> GenerateDocumentsOutput:
        """
        Sync files from GitLab attachments.
        """

        self.connector = GitlabConnector(
            project_owner=self.conf.get("project_owner"),
            project_name=self.conf.get("project_name"),
            batch_size=self.conf.get("batch_size", INDEX_BATCH_SIZE),
            state_filter=self.conf.get("state_filter", "all"),
            include_mrs=self.conf.get("include_mrs", False),
            include_issues=self.conf.get("include_issues", False),
            include_code_files=self.conf.get("include_code_files", False),
        )

        self.connector.load_credentials(
            {
                "gitlab_access_token": self.conf.get("credentials", {}).get("gitlab_access_token"),
                "gitlab_url": self.conf.get("gitlab_url"),
            }
        )

        if task["reindex"] == "1" or not task["poll_range_start"]:
            document_generator = self.connector.load_from_state()
            _begin_info = "totally"
        else:
            poll_start = task["poll_range_start"]
            if poll_start is None:
                document_generator = self.connector.load_from_state()
                _begin_info = "totally"
            else:
                document_generator = self.connector.poll_source(poll_start.timestamp(), datetime.now(UTC).timestamp())
                _begin_info = f"from {poll_start}"
        logging.info("Connect to Gitlab: ({}) {}".format(self.conf["project_name"], _begin_info))
        return document_generator


class Bitbucket(SyncBase):
    SOURCE_NAME: str = FileSource.BITBUCKET

    async def _generate(self, task: dict[str, Any]) -> GenerateDocumentsOutput:
        self.connector = BitbucketConnector(
            workspace=self.conf.get("workspace"),
            repositories=self.conf.get("repository_slugs"),
            projects=self.conf.get("projects"),
        )

        self.connector.load_credentials(
            {
                "bitbucket_email": self.conf["credentials"].get("bitbucket_account_email"),
                "bitbucket_api_token": self.conf["credentials"].get("bitbucket_api_token"),
            }
        )

        if task["reindex"] == "1" or not task["poll_range_start"]:
            start_time = datetime.fromtimestamp(0, tz=UTC)
            _begin_info = "totally"
        else:
            start_time = task.get("poll_range_start")
            _begin_info = f"from {start_time}"

        end_time = datetime.now(UTC)

        def document_batches() -> Iterator[list[Document]]:
            checkpoint = self.connector.build_dummy_checkpoint()

            while checkpoint.has_more:
                gen = self.connector.load_from_checkpoint(start=start_time.timestamp(), end=end_time.timestamp(), checkpoint=checkpoint)

                while True:
                    try:
                        item = next(gen)
                        if isinstance(item, ConnectorFailure):
                            raise RuntimeError(f"Bitbucket ingestion failed: {item.failure_message}")
                        yield [item]
                    except StopIteration as e:
                        checkpoint = e.value
                        break

        def wrapper() -> Iterator[list[Document]]:
            yield from document_batches()

        logging.info(
            "Connect to Bitbucket: workspace(%s), %s",
            self.conf.get("workspace"),
            _begin_info,
        )

        return wrapper()


class SeaFile(SyncBase):
    SOURCE_NAME: str = FileSource.SEAFILE

    async def _generate(self, task: dict[str, Any]) -> GenerateDocumentsOutput:
        conf = self.conf
        self.connector = SeaFileConnector(
            seafile_url=conf["seafile_url"],
            batch_size=conf.get("batch_size", INDEX_BATCH_SIZE),
            include_shared=conf.get("include_shared", True),
            sync_scope=conf.get("sync_scope", SeafileSyncScope.ACCOUNT),
            repo_id=conf.get("repo_id") or None,
            sync_path=conf.get("sync_path") or None,
        )
        self.connector.load_credentials(conf["credentials"])

        poll_start = task.get("poll_range_start")
        if task["reindex"] == "1" or poll_start is None:
            document_generator = self.connector.load_from_state()
            _begin_info = "totally"
        else:
            document_generator = self.connector.poll_source(
                poll_start.timestamp(),
                datetime.now(UTC).timestamp(),
            )
            _begin_info = f"from {poll_start}"

        scope = conf.get("sync_scope", "account")
        extra = ""
        if scope in ("library", "directory"):
            extra = f" repo_id={conf.get('repo_id')}"
        if scope == "directory":
            extra += f" path={conf.get('sync_path')}"

        logging.info(
            "Connect to SeaFile: %s (scope=%s%s) %s",
            conf["seafile_url"],
            scope,
            extra,
            _begin_info,
        )
        return document_generator


class DingTalkAITable(SyncBase):
    SOURCE_NAME: str = FileSource.DINGTALK_AI_TABLE

    async def _generate(self, task: dict[str, Any]) -> GenerateDocumentsOutput:
        """
        Sync records from DingTalk AI Table (Notable).
        """
        try:
            batch_size = int(self.conf.get("batch_size", INDEX_BATCH_SIZE))
        except (TypeError, ValueError):
            batch_size = INDEX_BATCH_SIZE
        if batch_size <= 0:
            batch_size = INDEX_BATCH_SIZE
        self.connector = DingTalkAITableConnector(
            table_id=self.conf.get("table_id"),
            operator_id=self.conf.get("operator_id"),
            batch_size=batch_size,
        )

        credentials = self.conf.get("credentials", {})
        if "access_token" not in credentials:
            raise ValueError("Missing access_token in credentials")

        self.connector.load_credentials({"access_token": credentials["access_token"]})

        poll_start = task.get("poll_range_start")

        if task.get("reindex") == "1" or poll_start is None:
            document_generator = self.connector.load_from_state()
            _begin_info = "totally"
        else:
            document_generator = self.connector.poll_source(
                poll_start.timestamp(),
                datetime.now(UTC).timestamp(),
            )
            _begin_info = f"from {poll_start}"

        logging.info(
            "Connect to DingTalk AI Table: table_id(%s), operator_id(%s) %s",
            self.conf.get("table_id"),
            self.conf.get("operator_id"),
            _begin_info,
        )

        return document_generator


class REST_API(SyncBase):
    SOURCE_NAME: str = FileSource.REST_API

    async def _generate(self, task: dict[str, Any]) -> GenerateDocumentsOutput:
        cfg = RestAPIConnector.parse_storage_config(self.conf)
        self.connector = RestAPIConnector.from_parsed_config(cfg)
        self.connector.load_credentials(self.conf.get("credentials") or {})
        start = task.get("poll_range_start")
        if task.get("reindex") == "1" or start is None:
            return self.connector.load_from_state()
        return self.connector.poll_source(start.timestamp(), datetime.now(UTC).timestamp())


class _RDBMSBase(SyncBase):
    DEFAULT_PORT: int = 0

    async def _generate(self, task: dict[str, Any]) -> GenerateDocumentsOutput:
        self.connector = RDBMSConnector(
            db_type=self.SOURCE_NAME,
            host=self.conf.get("host", "localhost"),
            port=int(self.conf.get("port") or self.DEFAULT_PORT),
            database=self.conf.get("database", ""),
            query=self.conf.get("query", ""),
            content_columns=self.conf.get("content_columns", ""),
            metadata_columns=self.conf.get("metadata_columns", ""),
            id_column=self.conf.get("id_column") or None,
            timestamp_column=self.conf.get("timestamp_column") or None,
            batch_size=int(self.conf.get("batch_size") or INDEX_BATCH_SIZE),
        )
        self.connector.load_credentials(self.conf.get("credentials") or {})
        self.connector.scope_signature = rdbms_scope_signature(self.conf, self.SOURCE_NAME)
        start = None
        scopes = self.conf.get("rdbms_sync_scopes", {})
        if not isinstance(scopes, dict):
            raise ValueError("Invalid persisted RDBMS synchronization state")
        saved = scopes.get(task["kb_id"])
        if saved is not None and task.get("reindex") != "1":
            if not isinstance(saved, dict) or saved.get("scope") != self.connector.scope_signature or "cursor" not in saved:
                raise ValueError("RDBMS query/account/column scope changed; rebuild this knowledge base")
            start = self.connector.deserialize_cursor_value(saved["cursor"])
        # A fresh/rebuilt KB never borrows another KB's cursor or the generic
        # wall-clock poll_range_start. Legacy tasks conservatively replay rows.
        if not task.get("poll_range_start") or task.get("reindex") == "1":
            start = None
        if self.connector.timestamp_column and self.connector.query:
            self.connector.pending_cursor = await asyncio.to_thread(self.connector.get_max_cursor_value)
            return self.connector.load_from_cursor_range(start, self.connector.pending_cursor)
        return self.connector.load_from_state()

    def _completion_state(self) -> dict[str, Any]:
        return {"source": self.SOURCE_NAME, "scope": self.connector.scope_signature, "cursor": self.connector.serialize_cursor_value(self.connector.pending_cursor)}


class MySQL(_RDBMSBase):
    SOURCE_NAME: str = FileSource.MYSQL
    DEFAULT_PORT = 3306


class PostgreSQL(_RDBMSBase):
    SOURCE_NAME: str = FileSource.POSTGRESQL
    DEFAULT_PORT = 5432


func_factory = {
    FileSource.REST_API: REST_API,
    FileSource.RSS: RSS,
    FileSource.S3: S3,
    FileSource.R2: R2,
    FileSource.OCI_STORAGE: OCI_STORAGE,
    FileSource.GOOGLE_CLOUD_STORAGE: GOOGLE_CLOUD_STORAGE,
    FileSource.NOTION: Notion,
    FileSource.DISCORD: Discord,
    FileSource.CONFLUENCE: Confluence,
    FileSource.GMAIL: Gmail,
    FileSource.GOOGLE_DRIVE: GoogleDrive,
    FileSource.JIRA: Jira,
    FileSource.SHAREPOINT: SharePoint,
    FileSource.SLACK: Slack,
    FileSource.TEAMS: Teams,
    FileSource.MOODLE: Moodle,
    FileSource.DROPBOX: Dropbox,
    FileSource.WEBDAV: WebDAV,
    FileSource.BOX: BOX,
    FileSource.AIRTABLE: Airtable,
    FileSource.ASANA: Asana,
    FileSource.IMAP: IMAP,
    FileSource.ZENDESK: Zendesk,
    FileSource.GITHUB: Github,
    FileSource.GITLAB: Gitlab,
    FileSource.BITBUCKET: Bitbucket,
    FileSource.SEAFILE: SeaFile,
    FileSource.MYSQL: MySQL,
    FileSource.POSTGRESQL: PostgreSQL,
    FileSource.DINGTALK_AI_TABLE: DingTalkAITable,
}


async def _gather_connector_tasks(tasks: list[asyncio.Task[None]]) -> None:
    """Wait for every connector task without letting one failure cancel its peers."""
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            logging.error("Unhandled connector task failure", exc_info=(type(result), result, result.__traceback__))


async def dispatch_tasks():
    while True:
        try:
            with db_connection() as db:
                list(SyncLogsService.list_sync_tasks(db)[0])
            break
        except Exception as e:
            logging.warning(f"DB is not ready yet: {e}")
            await asyncio.sleep(3)

    tasks = []
    with db_connection() as db:
        for task in SyncLogsService.list_sync_tasks(db)[0]:
            if task["poll_range_start"]:
                task["poll_range_start"] = task["poll_range_start"].astimezone(UTC)
            if task["poll_range_end"]:
                task["poll_range_end"] = task["poll_range_end"].astimezone(UTC)

            func = func_factory[task["source"]](task["config"])
            tasks.append(asyncio.create_task(func(task)))

    await _gather_connector_tasks(tasks)
    await asyncio.sleep(1)


stop_event = threading.Event()


def signal_handler(sig, frame):
    logging.info("Received interrupt signal, shutting down...")
    stop_event.set()
    time.sleep(1)
    sys.exit(0)


CONSUMER_NO = "0" if len(sys.argv) < 2 else sys.argv[1]
CONSUMER_NAME = "data_sync_" + CONSUMER_NO


async def main():
    # 装饰性 banner 走 stdout（多行 ASCII art 进 JSON 日志不可读）
    print(r"""
  _____        _           _____
 |  __ \      | |         / ____|
 | |  | | __ _| |_ __ _  | (___  _   _ _ __   ___
 | |  | |/ _` | __/ _` |  \___ \| | | | '_ \ / __|
 | |__| | (_| | || (_| |  ____) | |_| | | | | (__
 |_____/ \__,_|\__\__,_| |_____/ \__, |_| |_|\___|
                                  __/ |
                                 |___/
    """)
    logging.info(f"MultiRAG data sync version: {get_multirag_version()}")
    show_configs()
    settings.init_settings()
    if sys.platform != "win32":
        signal.signal(signal.SIGUSR1, start_tracemalloc_and_snapshot)
        signal.signal(signal.SIGUSR2, stop_tracemalloc)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logging.info(f"MultiRAG data sync is ready after {time.time() - start_ts}s initialization.")
    while not stop_event.is_set():
        try:
            await dispatch_tasks()
        except Exception:
            logging.exception("Data sync dispatch cycle failed; retrying")
            await asyncio.sleep(3)
    logging.error("BUG!!! You should not reach here!!!")


if __name__ == "__main__":
    faulthandler.enable()
    init_root_logger(CONSUMER_NAME)
    asyncio.run(main())
