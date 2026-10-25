"""Slack connector"""

import itertools
import logging
import math
import re
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from http.client import IncompleteRead, RemoteDisconnected
from typing import Any, cast
from urllib.error import URLError

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry import ConnectionErrorRetryHandler
from slack_sdk.http_retry.builtin_interval_calculators import FixedValueRetryIntervalCalculator

from common.data_source.config import _SLACK_LIMIT, ENABLE_EXPENSIVE_EXPERT_CALLS, FAST_TIMEOUT, INDEX_BATCH_SIZE, MAX_CHANNELS_TO_LOG, MAX_RETRIES, SLACK_NUM_THREADS
from common.data_source.exceptions import ConnectorMissingCredentialError, ConnectorValidationError, CredentialExpiredError, InsufficientPermissionsError, UnexpectedValidationError
from common.data_source.interfaces import CheckpointedConnectorWithPermSync, CredentialsConnector, SlimConnectorWithPermSync
from common.data_source.models import (
    BasicExpertInfo,
    ChannelType,
    ConnectorCheckpoint,
    ConnectorFailure,
    Document,
    GenerateSlimDocumentOutput,
    MessageType,
    ProcessedSlackMessage,
    SecondsSinceUnixEpoch,
    SlackMessageFilterReason,
    SlimDocument,
    ThreadType,
)
from common.data_source.utils import SlackTextCleaner, expert_info_from_slack_id

# Disallowed message subtypes list
_DISALLOWED_MSG_SUBTYPES = {
    "channel_join",
    "channel_leave",
    "channel_archive",
    "channel_unarchive",
    "pinned_item",
    "unpinned_item",
    "ekm_access_denied",
    "channel_posting_permissions",
    "group_join",
    "group_leave",
    "group_archive",
    "group_unarchive",
    "channel_name",
}


def default_msg_filter(message: MessageType) -> SlackMessageFilterReason | None:
    """Default message filter"""
    # Filter bot messages
    if message.get("bot_id") or message.get("app_id"):
        bot_profile_name = message.get("bot_profile", {}).get("name")
        if bot_profile_name == "DanswerBot Testing":
            return None
        return SlackMessageFilterReason.BOT

    # Filter non-informative content
    if message.get("subtype", "") in _DISALLOWED_MSG_SUBTYPES:
        return SlackMessageFilterReason.DISALLOWED

    return None


def _pages(call: Callable[..., Any], field: str, check_active: Callable[[], None] | None = None, **kwargs: Any) -> Generator[dict[str, Any], None, None]:
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        if check_active is not None:
            check_active()
        response = call(cursor=cursor, limit=_SLACK_LIMIT, **kwargs)
        response.validate()
        payload = response.data
        if not isinstance(payload, dict) or not isinstance(payload.get(field), list) or any(not isinstance(item, dict) for item in payload[field]):
            raise ConnectorValidationError("Malformed Slack collection response")
        metadata = payload.get("response_metadata", {})
        if not isinstance(metadata, dict):
            raise ConnectorValidationError("Malformed Slack pagination metadata")
        next_cursor = metadata.get("next_cursor", "")
        if not isinstance(next_cursor, str) or next_cursor in seen:
            raise ConnectorValidationError("Slack pagination did not advance")
        has_more = payload.get("has_more", False)
        if type(has_more) is not bool or (has_more and not next_cursor):
            raise ConnectorValidationError("Incomplete Slack pagination")
        yield payload
        if not next_cursor:
            return
        seen.add(next_cursor)
        cursor = next_cursor


def _collect_paginated_channels(
    client: WebClient,
    exclude_archived: bool,
    channel_types: list[str],
    check_active: Callable[[], None] | None = None,
) -> list[ChannelType]:
    """收集分页的频道列表"""
    channels: list[ChannelType] = []
    for result in _pages(
        client.conversations_list,
        "channels",
        check_active,
        exclude_archived=exclude_archived,
        types=channel_types,
    ):
        channels.extend(result["channels"])

    return channels


def get_channels(
    client: WebClient,
    exclude_archived: bool = True,
    get_public: bool = True,
    get_private: bool = True,
    check_active: Callable[[], None] | None = None,
) -> list[ChannelType]:
    channel_types = []
    if get_public:
        channel_types.append("public_channel")
    if get_private:
        channel_types.append("private_channel")

    # A private-channel or permission error must not become a successful partial sync.
    channels = _collect_paginated_channels(client, exclude_archived, channel_types, check_active)
    identifiers: set[str] = set()
    for channel in channels:
        if not isinstance(channel.get("id"), str) or not channel["id"] or not isinstance(channel.get("name"), str) or not channel["name"] or channel["id"] in identifiers:
            raise ConnectorValidationError("Invalid or duplicate Slack channel")
        identifiers.add(channel["id"])
    return channels


def get_channel_messages(
    client: WebClient,
    channel: ChannelType,
    oldest: str | None = None,
    latest: str | None = None,
    callback: Any = None,
    check_active: Callable[[], None] | None = None,
) -> Generator[list[MessageType], None, None]:
    """Get all messages in a channel"""
    if check_active is not None:
        check_active()
    # Join channel so bot can access messages
    if not channel["is_member"]:
        client.conversations_join(
            channel=channel["id"],
        )
        logging.info(f"Successfully joined '{channel['name']}'")

    for result in _pages(
        client.conversations_history,
        "messages",
        check_active,
        channel=channel["id"],
        oldest=oldest,
        latest=latest,
        inclusive=True,
    ):
        if callback:
            if callback.should_stop():
                raise RuntimeError("get_channel_messages: Stop signal detected")

            callback.progress("get_channel_messages", 0)
        yield cast(list[MessageType], result["messages"])


def get_thread(client: WebClient, channel_id: str, thread_id: str, latest: str | None = None, check_active: Callable[[], None] | None = None) -> ThreadType:
    threads: list[MessageType] = []
    seen: set[str] = set()
    for result in _pages(client.conversations_replies, "messages", check_active, channel=channel_id, ts=thread_id, latest=latest, inclusive=True):
        for message in result["messages"]:
            ts = message.get("ts")
            if not isinstance(ts, str) or not ts or ts in seen:
                raise ConnectorValidationError("Invalid or duplicate Slack thread message")
            seen.add(ts)
            if latest is None or float(ts) <= float(latest):
                threads.append(message)
    if not threads:
        raise ConnectorValidationError("Slack returned an empty thread")
    return sorted(threads, key=lambda message: float(message["ts"]))


def get_latest_message_time(thread: ThreadType) -> datetime:
    timestamps = [float(message["ts"]) for message in thread]
    for message in thread:
        if "edited" in message:
            edited = message["edited"]
            if not isinstance(edited, dict) or not isinstance(edited.get("ts"), str):
                raise ConnectorValidationError("Invalid Slack message edit timestamp")
            timestamps.append(float(edited["ts"]))
    if any(not math.isfinite(value) or value < 0 for value in timestamps):
        raise ConnectorValidationError("Invalid Slack message timestamp")
    return datetime.fromtimestamp(max(timestamps), tz=UTC)


def _build_doc_id(channel_id: str, thread_ts: str) -> str:
    return f"{channel_id}__{thread_ts}"


def thread_to_doc(
    channel: ChannelType,
    thread: ThreadType,
    slack_cleaner: SlackTextCleaner,
    client: WebClient,
    user_cache: dict[str, BasicExpertInfo | None],
    channel_access: Any | None,
) -> Document:
    channel_id = channel["id"]

    initial_sender_expert_info = expert_info_from_slack_id(user_id=thread[0].get("user"), client=client, user_cache=user_cache)
    initial_sender_name = initial_sender_expert_info.get_semantic_name() if initial_sender_expert_info else "Unknown"

    valid_experts = None
    if ENABLE_EXPENSIVE_EXPERT_CALLS:
        all_sender_ids = [m.get("user") for m in thread]
        experts = [expert_info_from_slack_id(user_id=sender_id, client=client, user_cache=user_cache) for sender_id in all_sender_ids if sender_id]
        valid_experts = [expert for expert in experts if expert]

    cleaned_messages = [slack_cleaner.index_clean(cast(str, message["text"])) for message in thread]
    first_message = cleaned_messages[0]
    snippet = first_message[:50].rstrip() + "..." if len(first_message) > 50 else first_message
    doc_sem_id = f"{initial_sender_name} in #{channel['name']}: {snippet}".replace("\n", " ")
    blob = "\n\n".join(cleaned_messages).encode("utf-8")
    return Document(
        id=_build_doc_id(channel_id=channel_id, thread_ts=thread[0].get("thread_ts") or thread[0]["ts"]),
        source="slack",
        semantic_identifier=doc_sem_id,
        extension=".txt",
        blob=blob,
        size_bytes=len(blob),
        doc_updated_at=get_latest_message_time(thread),
        primary_owners=valid_experts,
        metadata={"Channel": channel["name"]},
        externale_access=channel_access,
    )


def filter_channels(
    all_channels: list[ChannelType],
    channels_to_connect: list[str] | None,
    regex_enabled: bool,
) -> list[ChannelType]:
    if channels_to_connect is None:
        return all_channels

    if regex_enabled:
        return [channel for channel in all_channels if any(re.fullmatch(channel_to_connect, channel["name"]) for channel_to_connect in channels_to_connect)]

    # Validate all specified channels are valid
    all_channel_names = {channel["name"] for channel in all_channels}
    for channel in channels_to_connect:
        if channel not in all_channel_names:
            raise ValueError(
                f"Channel '{channel}' not found in workspace. "
                f"Available channels (Showing {len(all_channel_names)} of "
                f"{min(len(all_channel_names), MAX_CHANNELS_TO_LOG)}): "
                f"{list(itertools.islice(all_channel_names, MAX_CHANNELS_TO_LOG))}"
            )

    return [channel for channel in all_channels if channel["name"] in channels_to_connect]


def _get_channel_by_id(client: WebClient, channel_id: str) -> ChannelType:
    response = client.conversations_info(
        channel=channel_id,
    )
    return cast(ChannelType, response["channel"])


def _get_messages(
    channel: ChannelType,
    client: WebClient,
    oldest: str | None = None,
    latest: str | None = None,
    limit: int = _SLACK_LIMIT,
) -> tuple[list[MessageType], bool]:
    """Get messages (Slack returns from newest to oldest)"""

    # Must join channel to read messages
    if not channel["is_member"]:
        try:
            client.conversations_join(
                channel=channel["id"],
                is_private=channel["is_private"],
            )
        except SlackApiError as e:
            if e.response["error"] == "is_archived":
                logging.warning(f"Channel {channel['name']} is archived. Skipping.")
                return [], False

            logging.exception(f"Error joining channel {channel['name']}")
            raise
        logging.info(f"Successfully joined '{channel['name']}'")

    response = client.conversations_history(
        channel=channel["id"],
        oldest=oldest,
        latest=latest,
        limit=limit,
    )
    response.validate()

    messages = cast(list[MessageType], response.get("messages", []))

    cursor = cast(dict[str, Any], response.get("response_metadata", {})).get("next_cursor", "")
    has_more = bool(cursor)
    return messages, has_more


def _message_to_doc(
    message: MessageType,
    client: WebClient,
    channel: ChannelType,
    slack_cleaner: SlackTextCleaner,
    user_cache: dict[str, BasicExpertInfo | None],
    seen_thread_ts: set[str],
    channel_access: Any | None,
    msg_filter_func: Callable[[MessageType], SlackMessageFilterReason | None] = default_msg_filter,
    latest: str | None = None,
    check_active: Callable[[], None] | None = None,
) -> tuple[Document | None, SlackMessageFilterReason | None]:
    """Convert message to document"""
    filtered_thread: ThreadType | None = None
    filter_reason: SlackMessageFilterReason | None = None
    thread_ts = message.get("thread_ts") or (message["ts"] if message.get("reply_count") else None)
    if thread_ts:
        # If thread_ts exists, need to process thread
        if thread_ts in seen_thread_ts:
            return None, None

        thread = get_thread(client=client, channel_id=channel["id"], thread_id=thread_ts, latest=latest, check_active=check_active)

        filtered_thread = []
        for message in thread:
            filter_reason = msg_filter_func(message)
            if filter_reason:
                continue

            filtered_thread.append(message)
    else:
        filter_reason = msg_filter_func(message)
        if filter_reason:
            return None, filter_reason

        filtered_thread = [message]

    if not filtered_thread:
        return None, filter_reason

    doc = thread_to_doc(
        channel=channel,
        thread=filtered_thread,
        slack_cleaner=slack_cleaner,
        client=client,
        user_cache=user_cache,
        channel_access=channel_access,
    )
    if thread_ts:
        doc.id = _build_doc_id(channel["id"], thread_ts)
    return doc, None


def _process_message(
    message: MessageType,
    client: WebClient,
    channel: ChannelType,
    slack_cleaner: SlackTextCleaner,
    user_cache: dict[str, BasicExpertInfo | None],
    seen_thread_ts: set[str],
    channel_access: Any | None,
    msg_filter_func: Callable[[MessageType], SlackMessageFilterReason | None] = default_msg_filter,
    latest: str | None = None,
    check_active: Callable[[], None] | None = None,
) -> ProcessedSlackMessage:
    thread_ts = message.get("thread_ts")
    thread_or_message_ts = thread_ts or message["ts"]
    doc, filter_reason = _message_to_doc(
        message=message,
        client=client,
        channel=channel,
        slack_cleaner=slack_cleaner,
        user_cache=user_cache,
        seen_thread_ts=seen_thread_ts,
        channel_access=channel_access,
        msg_filter_func=msg_filter_func,
        latest=latest,
        check_active=check_active,
    )
    return ProcessedSlackMessage(doc=doc, thread_or_message_ts=thread_or_message_ts, filter_reason=filter_reason, failure=None)


class SlackConnector(
    SlimConnectorWithPermSync,
    CredentialsConnector,
    CheckpointedConnectorWithPermSync,
):
    """Slack connector"""

    def __init__(
        self,
        channels: list[str] | None = None,
        channel_regex_enabled: bool = False,
        batch_size: int = INDEX_BATCH_SIZE,
        num_threads: int = SLACK_NUM_THREADS,
        use_redis: bool = False,  # Simplified version, not using Redis
        check_active: Callable[[], None] | None = None,
    ) -> None:
        if type(batch_size) is not int or batch_size <= 0:
            raise ConnectorValidationError("Slack batch_size must be a positive integer")
        if type(channel_regex_enabled) is not bool:
            raise ConnectorValidationError("Slack channel_regex_enabled must be a boolean")
        self._check_active = check_active or (lambda: None)
        self.channels = channels
        self.channel_regex_enabled = channel_regex_enabled
        self.batch_size = batch_size
        self.num_threads = num_threads
        self.client: WebClient | None = None
        self.fast_client: WebClient | None = None
        self.text_cleaner: SlackTextCleaner | None = None
        self.user_cache: dict[str, BasicExpertInfo | None] = {}
        self.credentials_provider: Any = None
        self.use_redis = use_redis

    @property
    def channels(self) -> list[str] | None:
        return self._channels

    @channels.setter
    def channels(self, channels: list[str] | None) -> None:
        if channels is not None and (not isinstance(channels, list) or any(not isinstance(channel, str) or not channel.strip().removeprefix("#") for channel in channels)):
            raise ConnectorValidationError("Slack channels must be null or a list of nonempty names")
        self._channels = None if channels is None else [channel.strip().removeprefix("#") for channel in channels]

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        """Load credentials"""
        raise NotImplementedError("Use set_credentials_provider with this connector.")

    def set_credentials_provider(self, credentials_provider: Any) -> None:
        """Set credentials provider"""
        credentials = credentials_provider.get_credentials()
        bot_token = credentials.get("slack_bot_token")
        if not isinstance(bot_token, str) or not bot_token.strip():
            raise ConnectorMissingCredentialError("Slack bot token is missing")

        # Simplified version, not using Redis
        connection_error_retry_handler = ConnectionErrorRetryHandler(
            max_retry_count=MAX_RETRIES,
            interval_calculator=FixedValueRetryIntervalCalculator(),
            error_types=[
                URLError,
                ConnectionResetError,
                RemoteDisconnected,
                IncompleteRead,
            ],
        )

        self.client = WebClient(token=bot_token, timeout=30, retry_handlers=[connection_error_retry_handler])

        # For fast response requests
        self.fast_client = WebClient(token=bot_token, timeout=FAST_TIMEOUT)
        self.text_cleaner = SlackTextCleaner(client=self.client)
        self.credentials_provider = credentials_provider
        self.user_cache = {}

    def retrieve_all_slim_docs_perm_sync(
        self,
        callback: Any = None,
    ) -> GenerateSlimDocumentOutput:
        if self.client is None:
            raise ConnectorMissingCredentialError("Slack")

        for documents in self._fetch_document_batches(callback=callback):
            yield [SlimDocument(id=document.id) for document in documents]

    def _fetch_document_batches(self, start: float = 0.0, end: float | None = None, callback: Any = None) -> Generator[list[Document], None, None]:
        if self.client is None or self.text_cleaner is None:
            raise ConnectorMissingCredentialError("Slack")
        end = datetime.now(UTC).timestamp() if end is None else end
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            raise ConnectorValidationError("Invalid Slack polling window")
        if self.channels == []:
            return
        channels = filter_channels(get_channels(self.client, check_active=self._check_active), self.channels, self.channel_regex_enabled)
        batch: list[Document] = []
        for channel in channels:
            seen_thread_ts: set[str] = set()
            # History excludes replies to old parents. Scan parents as well,
            # then filter by the complete thread's latest message time.
            for messages in get_channel_messages(self.client, channel, latest=str(end), callback=callback, check_active=self._check_active):
                for message in messages:
                    self._check_active()
                    ts = message.get("ts")
                    if not isinstance(ts, str) or not ts or not isinstance(message.get("text"), str):
                        raise ConnectorValidationError("Invalid Slack message")
                    if message.get("thread_ts") is not None and (not isinstance(message["thread_ts"], str) or not message["thread_ts"]):
                        raise ConnectorValidationError("Invalid Slack thread identity")
                    canonical_ts = message.get("thread_ts") or ts
                    if canonical_ts in seen_thread_ts:
                        continue
                    processed = _process_message(message, self.client, channel, self.text_cleaner, self.user_cache, seen_thread_ts, None, latest=str(end), check_active=self._check_active)
                    if processed.failure is not None:
                        raise ConnectorValidationError("Slack message processing failed: " + processed.failure.failure_message) from processed.failure.exception
                    seen_thread_ts.add(canonical_ts)
                    document = processed.doc
                    if document is not None and start < document.doc_updated_at.timestamp() <= end:
                        batch.append(document)
                        if len(batch) >= self.batch_size:
                            yield batch
                            batch = []
        if batch:
            yield batch

    def load_from_state(self) -> Generator[list[Document], None, None]:
        return self._fetch_document_batches()

    def poll_source(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch) -> Generator[list[Document], None, None]:
        return self._fetch_document_batches(start, end)

    def load_from_checkpoint(self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch, checkpoint: ConnectorCheckpoint) -> Generator[Document | ConnectorFailure, None, ConnectorCheckpoint]:
        if checkpoint.has_more:
            for batch in self.poll_source(start, end):
                yield from batch
        return ConnectorCheckpoint(has_more=False)

    def load_from_checkpoint_with_perm_sync(
        self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch, checkpoint: ConnectorCheckpoint
    ) -> Generator[Document | ConnectorFailure, None, ConnectorCheckpoint]:
        return self.load_from_checkpoint(start, end, checkpoint)

    def build_dummy_checkpoint(self) -> ConnectorCheckpoint:
        return ConnectorCheckpoint(has_more=True)

    def validate_checkpoint_json(self, checkpoint_json: str) -> ConnectorCheckpoint:
        return ConnectorCheckpoint.model_validate_json(checkpoint_json, strict=True)

    def validate_connector_settings(self) -> None:
        """Validate connector settings"""
        if self.fast_client is None:
            raise ConnectorMissingCredentialError("Slack credentials not loaded.")

        try:
            # 1) Validate workspace connection
            auth_response = self.fast_client.auth_test()
            if not auth_response.get("ok", False):
                error_msg = auth_response.get("error", "Unknown error from Slack auth_test")
                raise ConnectorValidationError(f"Failed Slack auth_test: {error_msg}")

            # 2) Confirm listing channels functionality works
            test_resp = self.fast_client.conversations_list(limit=1, types=["public_channel"])
            if not test_resp.get("ok", False):
                error_msg = test_resp.get("error", "Unknown error from Slack")
                if error_msg == "invalid_auth":
                    raise ConnectorValidationError(f"Invalid Slack bot token ({error_msg}).")
                elif error_msg == "not_authed":
                    raise CredentialExpiredError(f"Invalid or expired Slack bot token ({error_msg}).")
                raise UnexpectedValidationError(f"Slack API returned a failure: {error_msg}")

            users_response = self.fast_client.users_info(user="USLACKBOT")
            users_response.validate()
            if not users_response.get("ok", False):
                raise InsufficientPermissionsError("Slack token requires users:read")

        except SlackApiError as e:
            slack_error = e.response.get("error", "")
            if slack_error == "ratelimited":
                raise UnexpectedValidationError("Slack validation was rate limited; retry later") from e
            elif slack_error == "missing_scope":
                raise InsufficientPermissionsError(
                    "Slack bot token lacks the necessary scope to list/access channels. Please ensure your Slack app has 'channels:read' (and/or 'groups:read' for private channels)."
                )
            elif slack_error == "invalid_auth":
                raise CredentialExpiredError(f"Invalid Slack bot token ({slack_error}).")
            elif slack_error == "not_authed":
                raise CredentialExpiredError(f"Invalid or expired Slack bot token ({slack_error}).")
            raise UnexpectedValidationError(f"Unexpected Slack error '{slack_error}' during settings validation.")
        except ConnectorValidationError as e:
            raise e
        except Exception as e:
            raise UnexpectedValidationError(f"Unexpected error during Slack settings validation: {e}")
