"""Blob storage connector"""

import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import xxhash

from common.data_source.config import BLOB_STORAGE_SIZE_THRESHOLD, INDEX_BATCH_SIZE, BlobType, DocumentSource
from common.data_source.exceptions import ConnectorMissingCredentialError, ConnectorValidationError, CredentialExpiredError, InsufficientPermissionsError
from common.data_source.interfaces import FingerprintConnector, LoadConnector, OnyxExtensionType, PollConnector
from common.data_source.models import Document, GenerateDocumentsOutput, GenerateSlimDocumentOutput, KeyRecord, SecondsSinceUnixEpoch, SlimDocument
from common.data_source.utils import (
    create_s3_client,
    detect_bucket_region,
    download_object_with_metadata,
    extract_size_bytes,
    get_file_ext,
    is_accepted_file_ext,
)


def _normalize_etag(raw_etag: str | None) -> str | None:
    token = raw_etag.strip().strip('"') if raw_etag else ""
    return xxhash.xxh128(token.encode()).hexdigest() if token else None


class BlobStorageConnector(LoadConnector, PollConnector, FingerprintConnector):
    """Blob storage connector"""

    def __init__(
        self,
        bucket_type: str,
        bucket_name: str,
        prefix: str = "",
        batch_size: int = INDEX_BATCH_SIZE,
        european_residency: bool = False,
    ) -> None:
        self.bucket_type: BlobType = BlobType(bucket_type)
        self.bucket_name = bucket_name.strip()
        self.prefix = prefix if not prefix or prefix.endswith("/") else prefix + "/"
        self.batch_size = batch_size
        self.s3_client: Any | None = None
        self._allow_images: bool | None = None
        self.size_threshold: int | None = BLOB_STORAGE_SIZE_THRESHOLD
        self.bucket_region: str | None = None
        self.european_residency: bool = european_residency
        self._listing_cache: dict[str, dict[str, Any]] = {}
        self._filename_counts: dict[str, int] = {}

    def set_allow_images(self, allow_images: bool) -> None:
        """Set whether to process images"""
        logging.info(f"Setting allow_images to {allow_images}.")
        self._allow_images = allow_images

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        """Load credentials"""
        logging.debug(f"Loading credentials for {self.bucket_name} of type {self.bucket_type}")

        # Validate credentials
        if self.bucket_type == BlobType.R2:
            if not all(credentials.get(key) for key in ["r2_access_key_id", "r2_secret_access_key", "account_id"]):
                raise ConnectorMissingCredentialError("Cloudflare R2")

        elif self.bucket_type == BlobType.S3:
            authentication_method = credentials.get("authentication_method", "access_key")

            if authentication_method == "access_key":
                if not all(credentials.get(key) for key in ["aws_access_key_id", "aws_secret_access_key"]):
                    raise ConnectorMissingCredentialError("Amazon S3")

            elif authentication_method == "iam_role":
                if not credentials.get("aws_role_arn"):
                    raise ConnectorMissingCredentialError("Amazon S3 IAM role ARN is required")

            elif authentication_method == "assume_role":
                pass

            else:
                raise ConnectorMissingCredentialError("Unsupported S3 authentication method")

        elif self.bucket_type == BlobType.GOOGLE_CLOUD_STORAGE:
            if not all(credentials.get(key) for key in ["access_key_id", "secret_access_key"]):
                raise ConnectorMissingCredentialError("Google Cloud Storage")

        elif self.bucket_type == BlobType.OCI_STORAGE:
            if not all(credentials.get(key) for key in ["namespace", "region", "access_key_id", "secret_access_key"]):
                raise ConnectorMissingCredentialError("Oracle Cloud Infrastructure")

        elif self.bucket_type == BlobType.S3_COMPATIBLE:
            if not all(credentials.get(key) for key in ["endpoint_url", "aws_access_key_id", "aws_secret_access_key", "addressing_style"]):
                raise ConnectorMissingCredentialError("S3 Compatible Storage")

        else:
            raise ValueError(f"Unsupported bucket type: {self.bucket_type}")

        # Create S3 client
        self.s3_client = create_s3_client(self.bucket_type, credentials, self.european_residency)

        # Detect bucket region (only important for S3)
        if self.bucket_type == BlobType.S3:
            self.bucket_region = detect_bucket_region(self.s3_client, self.bucket_name)

        return None

    def _build_document_from_obj(self, obj: dict[str, Any], filename_counts: dict[str, int]) -> Document | None:
        if self.s3_client is None:
            raise ConnectorMissingCredentialError("Blob storage")
        key = obj["Key"]
        file_name = os.path.basename(key)
        size_bytes = extract_size_bytes(obj)
        if self.size_threshold is not None and isinstance(size_bytes, int) and size_bytes > self.size_threshold:
            logging.warning("%s exceeds size threshold of %s. Skipping.", file_name, self.size_threshold)
            return None

        blob, metadata = download_object_with_metadata(self.s3_client, self.bucket_name, key, self.size_threshold)
        if blob is None:
            return None
        # A write between LIST and GET must never pair the new body with the
        # old listing's ETag. Missing response ETag deliberately falls back to
        # byte hashing at persistence, causing a conservative future fetch.
        last_modified = metadata.get("LastModified", obj["LastModified"]).replace(tzinfo=UTC)
        relative_path = key[len(self.prefix) :] if self.prefix and key.startswith(self.prefix) else key
        semantic_id = relative_path.replace("/", " / ") if filename_counts.get(file_name, 0) > 1 else file_name
        return Document(
            id=f"{self.bucket_type}:{self.bucket_name}:{key}",
            blob=blob,
            source=DocumentSource(self.bucket_type.value),
            semantic_identifier=semantic_id or file_name,
            extension=get_file_ext(file_name),
            doc_updated_at=last_modified,
            size_bytes=len(blob),
            fingerprint=_normalize_etag(metadata.get("ETag")),
        )

    def _yield_blob_objects(self, start: datetime, end: datetime) -> GenerateDocumentsOutput:
        all_objects, filename_counts = self._collect_blob_objects(start, end)
        batch: list[Document] = []
        for obj in all_objects:
            # Download/listing failures must fail the sync before deletion or
            # checkpoint completion. Only configured size exclusions may skip.
            doc = self._build_document_from_obj(obj, filename_counts)
            if doc is None:
                continue
            batch.append(doc)
            if len(batch) >= self.batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def list_keys(self) -> Iterator[KeyRecord]:
        self._listing_cache = {}
        self._filename_counts = {}
        all_objects, filename_counts = self._collect_blob_objects()
        # Publish the cache only after the full paginated listing succeeds.
        self._filename_counts = filename_counts
        self._listing_cache = {f"{self.bucket_type}:{self.bucket_name}:{obj['Key']}": obj for obj in all_objects}
        for key, obj in self._listing_cache.items():
            yield KeyRecord(key=key, fingerprint=_normalize_etag(obj.get("ETag")))

    def get_value(self, key: str) -> Document | None:
        return self._build_document_from_obj(self._listing_cache[key], self._filename_counts)

    def _collect_blob_objects(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Exhaust the configured prefix listing, preserving local file filters."""
        if self.s3_client is None:
            raise ConnectorMissingCredentialError("Blob storage")

        paginator = self.s3_client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=self.bucket_name, Prefix=self.prefix)
        extension_type = OnyxExtensionType.Plain | OnyxExtensionType.Document
        if bool(self._allow_images):
            extension_type |= OnyxExtensionType.Multimedia

        all_objects: list[dict[str, Any]] = []
        filename_counts: dict[str, int] = {}
        for page in pages:
            if page.get("IsTruncated") and not page.get("NextContinuationToken"):
                raise ConnectorValidationError("Blob listing is truncated without a continuation token.")
            if page.get("KeyCount", 0) and not page.get("Contents"):
                raise ConnectorValidationError("Blob listing reports keys without object metadata.")
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                file_name = os.path.basename(key)
                if not is_accepted_file_ext(get_file_ext(file_name), extension_type):
                    continue
                if start is not None or end is not None:
                    last_modified = obj["LastModified"].replace(tzinfo=UTC)
                    if start is not None and last_modified <= start:
                        continue
                    if end is not None and last_modified > end:
                        continue
                all_objects.append(obj)
                filename_counts[file_name] = filename_counts.get(file_name, 0) + 1

        return all_objects, filename_counts

    def retrieve_all_slim_docs_perm_sync(self, callback: Any = None) -> GenerateSlimDocumentOutput:
        """List existing source IDs without a time window or content download.

        Size and download limits affect ingestion, not source existence: a file
        that grows beyond the limit must not be mistaken for a deleted file.
        Listing failures propagate before a snapshot can be used for deletion.
        """
        del callback
        all_objects, _ = self._collect_blob_objects()
        batch: list[SlimDocument] = []
        for obj in all_objects:
            batch.append(SlimDocument(id=f"{self.bucket_type}:{self.bucket_name}:{obj['Key']}"))
            if len(batch) >= self.batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def load_from_state(self) -> GenerateDocumentsOutput:
        """Load documents from state"""
        logging.debug("Loading blob objects")
        return self._yield_blob_objects(
            start=datetime(1970, 1, 1, tzinfo=UTC),
            end=datetime.now(UTC),
        )

    def poll_source(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch) -> GenerateDocumentsOutput:
        """Poll source to get documents"""
        if self.s3_client is None:
            raise ConnectorMissingCredentialError("Blob storage")

        start_datetime = datetime.fromtimestamp(start, tz=UTC)
        end_datetime = datetime.fromtimestamp(end, tz=UTC)

        yield from self._yield_blob_objects(start_datetime, end_datetime)

    def validate_connector_settings(self) -> None:
        """Validate connector settings"""
        if self.s3_client is None:
            raise ConnectorMissingCredentialError("Blob storage credentials not loaded.")

        if not self.bucket_name:
            raise ConnectorValidationError("No bucket name was provided in connector settings.")

        try:
            # Lightweight validation step
            self.s3_client.list_objects_v2(Bucket=self.bucket_name, Prefix=self.prefix, MaxKeys=1)

        except Exception as e:
            error_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            status_code = getattr(e, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")

            # Common S3 error scenarios
            if error_code in [
                "AccessDenied",
                "InvalidAccessKeyId",
                "SignatureDoesNotMatch",
            ]:
                if status_code == 403 or error_code == "AccessDenied":
                    raise InsufficientPermissionsError(f"Insufficient permissions to list objects in bucket '{self.bucket_name}'. Please check your bucket policy and/or IAM policy.")
                if status_code == 401 or error_code == "SignatureDoesNotMatch":
                    raise CredentialExpiredError("Provided blob storage credentials appear invalid or expired.")

                raise CredentialExpiredError(f"Credential issue encountered ({error_code}).")

            if error_code == "NoSuchBucket" or status_code == 404:
                raise ConnectorValidationError(f"Bucket '{self.bucket_name}' does not exist or cannot be found.")

            raise ConnectorValidationError(f"Unexpected S3 client error (code={error_code}, status={status_code}): {e}")


if __name__ == "__main__":
    # Example usage
    credentials_dict = {
        "aws_access_key_id": os.environ.get("AWS_ACCESS_KEY_ID"),
        "aws_secret_access_key": os.environ.get("AWS_SECRET_ACCESS_KEY"),
    }

    # Initialize connector
    connector = BlobStorageConnector(
        bucket_type=os.environ.get("BUCKET_TYPE") or "s3",
        bucket_name=os.environ.get("BUCKET_NAME") or "yyboombucket",
        prefix="",
    )

    try:
        connector.load_credentials(credentials_dict)
        document_batch_generator = connector.load_from_state()
        for document_batch in document_batch_generator:
            print("First batch of documents:")
            for doc in document_batch:
                print(f"Document ID: {doc.id}")
                print(f"Semantic Identifier: {doc.semantic_identifier}")
                print(f"Source: {doc.source}")
                print(f"Updated At: {doc.doc_updated_at}")
                print("---")
            break

    except ConnectorMissingCredentialError as e:
        print(f"Error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
