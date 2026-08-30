"""Lifecycle bridge for trusted server-side Channel bindings, any provider."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import secrets
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from api.channel_capabilities import EffectiveReplyCapabilities
from api.channels.agent_bridge import (
    DEMO_ONLY_TEXT,
    QUESTION_TOO_LONG_TEXT,
    SESSION_RESET_TEXT,
    TEXT_ONLY_TEXT,
    AgentExecutionError,
)
from api.channels.core.base import (
    Channel,
    ChannelAction,
    ChannelActionResponse,
    ChannelFormAction,
    IncomingIdentityAssertion,
    IncomingMessage,
    InteractionReplySession,
    OutgoingMessage,
    ReplyActionIds,
    ReplyActionKind,
    ReplyContext,
    ReplySession,
    ReplySessionState,
    ReplyStatus,
)
from api.channels.core.reply import SERVICE_UNAVAILABLE_TEXT
from api.channels.execution_events import (
    BindingExecutionEvent,
    ExecutionFailedEvent,
    InteractionRequiredEvent,
    MessageCompletedEvent,
    MessageDeltaEvent,
)
from api.channels.interaction_models import ClaimedInteractionDelivery
from api.channels.state_store import ChannelStateStore, binding_conversation_key
from api.channels.telemetry import (
    NOOP_CHANNEL_TELEMETRY,
    ChannelMessageOutcome,
    ChannelOperation,
    ChannelReason,
    ChannelResult,
    ChannelStage,
    ChannelTelemetry,
    channel_operation,
    channel_provider,
)

LOGGER = logging.getLogger(__name__)

QUEUE_BUSY_TEXT = "当前会话排队较多，请稍后再试。"
SHUTDOWN_BUSY_TEXT = "服务正在停止，请稍后重试。"
_ACTION_TTL_SECONDS = 86_400
_MAX_ACTIONS = 4_096
# Cooperative shutdown budgets. Both are spent inside one worker close, and
# their sum stays under the supervisor's 10s wait before it escalates to a
# kill -- a killed process has no cleanup window at all, which is exactly the
# case this lifecycle refuses to claim it handles.
_SHUTDOWN_PREPARATION_GRACE_SECONDS = 2.0
_SHUTDOWN_FINALIZE_BUDGET_SECONDS = 5.0
_TERMINAL_REPLY_STATUSES = frozenset(
    {
        ReplyStatus.AWAITING_INPUT,
        ReplyStatus.FINAL,
        ReplyStatus.ERROR,
        ReplyStatus.CANCELLED,
    }
)
_INTERACTION_POLL_SECONDS = 0.5


@runtime_checkable
class BindingExecutor(Protocol):
    def stream(
        self,
        *,
        question: str,
        event_id: str,
        conversation_key: str,
        provider: str,
        subject: str,
        conversation: str,
        identity: IncomingIdentityAssertion | None = None,
        operation: Literal["message", "regenerate"] = "message",
        presentation_ref: str | None = None,
    ) -> AsyncGenerator[BindingExecutionEvent, None]: ...

    async def reset(self, *, conversation_key: str) -> None: ...


@runtime_checkable
class InteractionDeliveryClient(Protocol):
    async def claim_interaction_delivery(
        self,
        *,
        owner: str,
        action_id: str | None = None,
        revision: int | None = None,
    ) -> ClaimedInteractionDelivery | None: ...

    async def acknowledge_interaction_delivery(
        self,
        *,
        delivery: ClaimedInteractionDelivery,
        owner: str,
        success: bool,
        safe_error_code: str | None = None,
    ) -> None: ...

    async def receive_interaction_callback(
        self,
        action: ChannelFormAction,
    ) -> Literal["accepted", "duplicate"]: ...


@runtime_checkable
class InteractionPresenter(Protocol):
    async def present(self, delivery: ClaimedInteractionDelivery) -> None: ...


class _WorkKind(StrEnum):
    QUESTION = "question"
    DIRECT = "direct"
    RESET = "reset"
    EMPTY = "empty"


@dataclass(slots=True)
class _ExecutionRecord:
    source: IncomingMessage
    conversation_key: str
    question: str
    action_ids: ReplyActionIds
    capabilities: EffectiveReplyCapabilities
    status: ReplyStatus = ReplyStatus.QUEUED
    reply_session: ReplySession | None = None
    reply_message_id: str = ""
    execution_task: asyncio.Task[None] | None = None
    cancel_requested: bool = False
    feedback: bool | None = None
    telemetry_outcome: ChannelMessageOutcome | None = None
    telemetry_reason: ChannelReason = ChannelReason.NONE
    finalization_done: asyncio.Event = field(default_factory=asyncio.Event)
    finalization_outcome: ChannelMessageOutcome | None = None
    # A stop callback and a cooperative shutdown can both reach one reply. The
    # flag keeps the provider-visible terminal transition single even while the
    # first attempt is suspended on provider I/O.
    finalizing: bool = False


@dataclass(slots=True)
class _PreparedMessage:
    source: IncomingMessage
    conversation_key: str
    kind: _WorkKind
    direct_content: str = ""
    record: _ExecutionRecord | None = None
    outcome: ChannelMessageOutcome | None = None


@dataclass(slots=True)
class _RegisteredAction:
    record: _ExecutionRecord
    kind: ReplyActionKind
    expires_at: float
    used: bool = False


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class BindingBridge:
    """Own one message's prepare/run/terminal lifecycle.

    The worker is the only conversation queue owner. This bridge deliberately
    has no second per-conversation lock: accepted messages are prepared early
    so every source gets an immediate queued card, then the worker starts them
    in ticket order. Card callbacks only validate an opaque action, claim it in
    this single-leader process and enqueue asynchronous work.
    """

    def __init__(
        self,
        *,
        channel: Channel,
        executor: BindingExecutor,
        state_store: ChannelStateStore,
        binding_id: str,
        allowed_sender_ids: set[str] | frozenset[str],
        max_question_chars: int,
        max_answer_chars: int,
        capabilities: EffectiveReplyCapabilities,
        private_chat_only: bool = True,
        interaction_client: InteractionDeliveryClient | None = None,
        interaction_presenter: InteractionPresenter | None = None,
        telemetry: ChannelTelemetry = NOOP_CHANNEL_TELEMETRY,
        provider_name: str = "other",
    ) -> None:
        if (interaction_client is None) != (interaction_presenter is None):
            raise ValueError("interaction client and presenter must be configured together")
        self._channel = channel
        self._executor = executor
        self._state_store = state_store
        self._binding_id = binding_id
        self._allowed_sender_ids = frozenset(allowed_sender_ids)
        self._max_question_chars = max_question_chars
        self._max_answer_chars = max_answer_chars
        self._private_chat_only = private_chat_only
        self._capabilities = capabilities
        self._interaction_client = interaction_client
        self._interaction_presenter = interaction_presenter
        self._telemetry = telemetry
        self._provider = channel_provider(provider_name)
        self._interaction_owner = f"channel-{secrets.token_hex(12)}"
        self._interaction_revisions: dict[str, int] = {}
        self._interaction_wakeup = asyncio.Event()
        self._scheduler: Callable[[IncomingMessage], Awaitable[None]] | None = None
        self._preparations: dict[int, asyncio.Task[_PreparedMessage]] = {}
        self._actions: dict[str, _RegisteredAction] = {}
        self._latest_execution_by_conversation: dict[str, str] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        # Every reply whose card can still change. Membership -- not status --
        # is what a cooperative shutdown walks, so a reply that already crossed
        # its terminal barrier stays here until its delivery really finished.
        self._live_records: dict[str, _ExecutionRecord] = {}
        self._closing = False

    def set_message_scheduler(
        self,
        scheduler: Callable[[IncomingMessage], Awaitable[None]],
    ) -> None:
        self._scheduler = scheduler

    def accepts_message(self, message: IncomingMessage) -> bool:
        """Apply transport-only policy before the worker allocates a ticket."""

        return self.message_rejection_reason(message) is None

    def message_rejection_reason(
        self,
        message: IncomingMessage,
    ) -> ChannelReason | None:
        """Return one closed reason for a pre-queue policy rejection."""

        if self._closing:
            return ChannelReason.WORKER_STOPPING
        if self._private_chat_only and message.chat_type != "p2p":
            return ChannelReason.PRIVATE_CHAT_REQUIRED
        if message.sender_type != "user":
            return ChannelReason.USER_SENDER_REQUIRED
        if not message.message_id or not message.chat_id or not message.sender_id:
            self._log(logging.WARNING, "event_rejected", message, "MESSAGE_IDENTITY_MISSING")
            return ChannelReason.MESSAGE_IDENTITY_MISSING
        return None

    def message_queued(self, message: IncomingMessage, *, queue_position: int) -> None:
        """Start claim/card preparation without blocking the SDK callback."""

        if self._closing:
            return
        task = asyncio.create_task(
            self._prepare_message(
                message,
                queue_position=queue_position,
                telemetry_started_at=time.monotonic(),
            ),
            name=f"channel-prepare-{_short_hash(message.execution_id)}",
        )
        self._preparations[id(message)] = task

    def message_rejected(self, message: IncomingMessage, *, reason: str) -> None:
        """Make queue overflow user-visible; never silently drop it."""

        self._spawn_background(
            self._reply_queue_rejected(message, reason=reason),
            name=f"channel-reject-{_short_hash(message.execution_id)}",
        )

    async def handle_message(
        self,
        message: IncomingMessage,
    ) -> ChannelMessageOutcome:
        """Run a previously prepared message after its worker ticket starts."""

        if self._closing:
            # Intake already stopped, so this ticket must not reach the target.
            # ``close`` owns the terminal state of the card it already showed.
            return ChannelMessageOutcome(
                ChannelResult.DROPPED,
                ChannelReason.SHUTDOWN,
            )
        preparation = self._preparations.pop(id(message), None)
        if preparation is None:
            rejection_reason = self.message_rejection_reason(message)
            if rejection_reason is not None:
                return ChannelMessageOutcome(
                    ChannelResult.DROPPED,
                    rejection_reason,
                )
            preparation = asyncio.create_task(
                self._prepare_message(
                    message,
                    queue_position=0,
                    telemetry_started_at=time.monotonic(),
                )
            )
        prepared = await preparation
        if prepared.outcome is not None:
            return prepared.outcome
        if prepared.kind is _WorkKind.DIRECT:
            return await self._reply_and_complete(message, prepared.direct_content)
        if prepared.kind is _WorkKind.EMPTY:
            return await self._mark_replied(message)
        if prepared.kind is _WorkKind.RESET:
            return await self._run_reset(prepared)
        if prepared.record is not None:
            await self._run_question(prepared.record)
            return self._message_outcome(prepared.record)
        return ChannelMessageOutcome(
            ChannelResult.FAILED,
            ChannelReason.HANDLER_FAILURE,
        )

    async def handle_action(self, action: ChannelAction) -> ChannelActionResponse:
        """Claim a low-risk action and enqueue work without provider I/O."""

        if self._closing:
            return ChannelActionResponse("warning", SHUTDOWN_BUSY_TEXT)
        self._cleanup_actions()
        registered = self._actions.get(action.action_id)
        if registered is None or registered.expires_at <= time.monotonic():
            return ChannelActionResponse("warning", "操作已过期，请使用最新卡片。")
        if registered.used:
            return ChannelActionResponse("info", "该操作已经处理。")

        record = registered.record
        if action.operator_id != record.source.sender_id or action.chat_id != record.source.chat_id or (record.reply_message_id and action.message_id != record.reply_message_id):
            self._log(
                logging.WARNING,
                "action_rejected",
                record.source,
                "ACTION_IDENTITY_MISMATCH",
            )
            return ChannelActionResponse("error", "你不能操作这张卡片。")

        if registered.kind is ReplyActionKind.CANCEL:
            if record.status not in {ReplyStatus.QUEUED, ReplyStatus.RUNNING}:
                registered.used = True
                return ChannelActionResponse("info", "生成已经结束。")
            can_cancel = record.capabilities.cancel_queued if record.status is ReplyStatus.QUEUED else record.capabilities.cancel_running
            if not can_cancel:
                registered.used = True
                return ChannelActionResponse("warning", "当前阶段不支持停止生成。")
            registered.used = True
            record.cancel_requested = True
            task = record.execution_task
            if task is not None and not task.done():
                task.cancel()
            elif record.reply_session is not None:
                self._spawn_background(
                    self._finalize_cancel(record),
                    name=f"channel-cancel-{_short_hash(record.source.execution_id)}",
                )
            return ChannelActionResponse("success", "正在停止生成。")

        if registered.kind in {ReplyActionKind.REGENERATE, ReplyActionKind.RETRY}:
            replace_completed = registered.kind is ReplyActionKind.REGENERATE
            action_label = "重新生成" if replace_completed else "重试"
            latest_subject = "回答" if replace_completed else "请求"
            task_kind = "regenerate" if replace_completed else "retry"
            capability_enabled = record.capabilities.regenerate if replace_completed else record.capabilities.retry
            if not capability_enabled:
                registered.used = True
                return ChannelActionResponse("warning", "当前回答不支持该操作。")
            expected_statuses = {ReplyStatus.FINAL} if replace_completed else {ReplyStatus.ERROR, ReplyStatus.CANCELLED}
            if record.status not in expected_statuses:
                return ChannelActionResponse("warning", "请等待当前生成结束。")
            if self._scheduler is None:
                return ChannelActionResponse("error", f"{action_label}暂时不可用。")
            if self._latest_execution_by_conversation.get(record.conversation_key) != record.source.execution_id:
                registered.used = True
                return ChannelActionResponse("warning", f"只能{action_label}当前会话的最新{latest_subject}。")
            registered.used = True
            regenerated = self._regenerated_message(
                record.source,
                action,
                replace_completed=replace_completed,
            )
            self._spawn_background(
                self._scheduler(regenerated),
                name=f"channel-{task_kind}-{_short_hash(regenerated.execution_id)}",
            )
            return ChannelActionResponse("success", "已加入当前会话队列。")

        if record.status is not ReplyStatus.FINAL:
            return ChannelActionResponse("warning", "回答尚未完成。")
        if not record.capabilities.feedback:
            registered.used = True
            return ChannelActionResponse("warning", "当前回答不支持反馈。")
        if record.feedback is not None:
            self._mark_feedback_actions_used(record)
            return ChannelActionResponse("info", "感谢，你已经反馈过了。")

        helpful = registered.kind is ReplyActionKind.HELPFUL
        record.feedback = helpful
        self._mark_feedback_actions_used(record)
        self._spawn_background(
            self._acknowledge_feedback(record, helpful=helpful),
            name=f"channel-feedback-{_short_hash(record.source.execution_id)}",
        )
        return ChannelActionResponse("success", "感谢反馈。")

    async def handle_form_action(
        self,
        action: ChannelFormAction,
    ) -> ChannelActionResponse:
        """Durably receipt a form callback; rendering and MCP stay asynchronous."""

        if self._closing:
            return ChannelActionResponse("warning", SHUTDOWN_BUSY_TEXT)
        client = self._interaction_client
        if client is None:
            return ChannelActionResponse("warning", "该表单当前不可用。")
        try:
            status = await client.receive_interaction_callback(action)
        except AgentExecutionError as exc:
            if exc.code.endswith("HTTP_404") or exc.code.endswith("HTTP_409"):
                return ChannelActionResponse(
                    "warning",
                    "表单已过期或不是最新版本，请刷新后重试。",
                )
            return ChannelActionResponse("error", "提交未保存，请稍后重试。")
        self._interaction_revisions[action.action_id] = action.revision
        self._interaction_wakeup.set()
        if status == "duplicate":
            return ChannelActionResponse("info", "该提交已经收到，正在处理。")
        return ChannelActionResponse("success", "提交已收到，正在处理。")

    async def run_interaction_deliveries(
        self,
        stop_event: asyncio.Event,
    ) -> None:
        """Deliver follow-up rounds and terminal pages from the durable outbox."""

        if self._interaction_client is None or self._interaction_presenter is None:
            await stop_event.wait()
            return
        while not stop_event.is_set() and not self._closing:
            delivered = False
            try:
                delivered = await self._poll_interaction_delivery()
            except asyncio.CancelledError:
                raise
            except AgentExecutionError as exc:
                if exc.code.endswith("HTTP_503"):
                    # The API is deliberately replaced during staged rollout
                    # and waiting-input recovery.  A single unavailable poll
                    # must not permanently orphan the durable delivery outbox
                    # until the whole Channel worker is restarted.
                    pass
                else:
                    LOGGER.warning(
                        "channel_event=interaction_delivery_poll result=failed error_code=%s",
                        exc.code,
                    )
            except Exception as exc:
                LOGGER.warning(
                    "channel_event=interaction_delivery_poll result=failed error_code=CHANNEL_INTERACTION_POLL_FAILED error_type=%s",
                    type(exc).__name__,
                )
            if delivered:
                continue
            self._interaction_wakeup.clear()
            try:
                await asyncio.wait_for(
                    self._interaction_wakeup.wait(),
                    timeout=_INTERACTION_POLL_SECONDS,
                )
            except TimeoutError:
                pass

    async def close(self) -> None:
        """Give every visible reply one terminal state, then drop bridge tasks.

        This is the cooperative window only: the caller has already stopped
        intake, so nothing queued may still reach the target. A reply that
        never started is cancelled without touching the execution client, a
        running reply has its stream closed and is cancelled, and a reply that
        already crossed its terminal barrier is allowed to finish delivering.
        Repeat calls are no-ops. Nothing here survives the process: a kill -9
        still leaves the queue, the records and the cards where they were.
        """

        self._closing = True
        await self._settle_preparations()
        await self._finalize_visible_replies()

        tasks = [*self._preparations.values(), *self._background_tasks]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._preparations.clear()
        self._background_tasks.clear()
        self._actions.clear()
        self._interaction_revisions.clear()
        self._interaction_wakeup.set()
        self._latest_execution_by_conversation.clear()
        self._live_records.clear()

    async def _settle_preparations(self) -> None:
        """Let in-flight card creation land so its card can be terminalized.

        A preparation cancelled between ``create`` and ``reply`` would leave a
        card nobody holds a handle to, which is the one shape this lifecycle
        cannot repair. Waiting a bounded moment is cheaper than that.
        """

        pending = [task for task in self._preparations.values() if not task.done()]
        if not pending:
            return
        await asyncio.wait(pending, timeout=_SHUTDOWN_PREPARATION_GRACE_SECONDS)

    async def _finalize_visible_replies(self) -> None:
        records = list(self._live_records.values())
        if not records:
            return
        queued = sum(1 for record in records if record.status is ReplyStatus.QUEUED)
        running = sum(1 for record in records if record.status is ReplyStatus.RUNNING)
        outcomes: list[ChannelMessageOutcome] = []
        try:
            async with asyncio.timeout(_SHUTDOWN_FINALIZE_BUDGET_SECONDS):
                for record in records:
                    outcome = await self._finalize_on_shutdown(record)
                    if outcome is not None:
                        outcomes.append(outcome)
        except TimeoutError:
            self._telemetry.shutdown(
                provider=self._provider,
                result=ChannelResult.FAILED,
                reason=ChannelReason.SHUTDOWN_TIMEOUT,
                queued=queued,
                running=running,
            )
            LOGGER.error(
                "channel_event=shutdown_finalized binding_id_hash=%s queued=%s running=%s result=failed error_code=SHUTDOWN_FINALIZE_TIMEOUT",
                _short_hash(self._binding_id),
                queued,
                running,
            )
            return
        failed = next(
            (outcome for outcome in outcomes if outcome.result is ChannelResult.FAILED),
            None,
        )
        if failed is not None:
            self._telemetry.shutdown(
                provider=self._provider,
                result=ChannelResult.FAILED,
                reason=failed.reason,
                queued=queued,
                running=running,
            )
            LOGGER.error(
                "channel_event=shutdown_finalized binding_id_hash=%s queued=%s running=%s result=failed error_code=SHUTDOWN_FINALIZE_FAILED reason=%s",
                _short_hash(self._binding_id),
                queued,
                running,
                failed.reason.value,
            )
            return
        self._telemetry.shutdown(
            provider=self._provider,
            result=ChannelResult.OK,
            reason=ChannelReason.NONE,
            queued=queued,
            running=running,
        )
        LOGGER.info(
            "channel_event=shutdown_finalized binding_id_hash=%s queued=%s running=%s result=ok error_code=",
            _short_hash(self._binding_id),
            queued,
            running,
        )

    async def _finalize_on_shutdown(
        self,
        record: _ExecutionRecord,
    ) -> ChannelMessageOutcome | None:
        task = record.execution_task
        outcome: ChannelMessageOutcome | None = None
        if record.status in {ReplyStatus.QUEUED, ReplyStatus.RUNNING}:
            # Cancel and finalize with no await in between: once the execution
            # task is cancelled it can no longer append to this reply, so the
            # terminal card write below is uncontended. A queued record has no
            # task at all, which is why this path reaches the executor zero
            # times. ``cancel_requested`` tells ``_run_question`` that the
            # unwind is a shutdown rather than a stream that failed by itself.
            record.cancel_requested = True
            record.telemetry_reason = ChannelReason.SHUTDOWN
            if task is not None and not task.done():
                task.cancel()
            outcome = await self._finalize_cancel(record)
        if task is not None and not task.done():
            # Reached for a record past its terminal barrier, which was not
            # cancelled above: it holds a real answer from a target that
            # already committed, so the honest move is to let its delivery
            # finish. For a cancelled task this is what closes the stream.
            await asyncio.gather(task, return_exceptions=True)
        return outcome or record.telemetry_outcome

    async def _prepare_message(
        self,
        message: IncomingMessage,
        *,
        queue_position: int,
        telemetry_started_at: float,
    ) -> _PreparedMessage:
        try:
            claimed = await self._state_store.claim_message(message.execution_id)
        except Exception:
            self._log(logging.ERROR, "state_failed", message, "REDIS_CLAIM_FAILED")
            await self._safe_reply(message, SERVICE_UNAVAILABLE_TEXT)
            return _PreparedMessage(
                source=message,
                conversation_key="",
                kind=_WorkKind.EMPTY,
                outcome=ChannelMessageOutcome(
                    ChannelResult.FAILED,
                    ChannelReason.STATE_FAILURE,
                ),
            )
        if not claimed:
            self._log(logging.INFO, "duplicate_dropped", message, "", result="duplicate")
            return _PreparedMessage(
                source=message,
                conversation_key="",
                kind=_WorkKind.EMPTY,
                outcome=ChannelMessageOutcome(
                    ChannelResult.DUPLICATE,
                    ChannelReason.DUPLICATE,
                ),
            )

        conversation = binding_conversation_key(
            self._binding_id,
            message.channel,
            message.chat_id,
            message.sender_id,
        )
        if self._allowed_sender_ids and message.sender_id not in self._allowed_sender_ids:
            return _PreparedMessage(
                source=message,
                conversation_key=conversation,
                kind=_WorkKind.DIRECT,
                direct_content=DEMO_ONLY_TEXT,
            )
        if message.message_type != "text":
            return _PreparedMessage(
                source=message,
                conversation_key=conversation,
                kind=_WorkKind.DIRECT,
                direct_content=TEXT_ONLY_TEXT,
            )

        question = message.text.strip()
        if not question:
            return _PreparedMessage(
                source=message,
                conversation_key=conversation,
                kind=_WorkKind.EMPTY,
            )
        if len(question) > self._max_question_chars:
            return _PreparedMessage(
                source=message,
                conversation_key=conversation,
                kind=_WorkKind.DIRECT,
                direct_content=QUESTION_TOO_LONG_TEXT,
            )
        if question == "/reset":
            return _PreparedMessage(
                source=message,
                conversation_key=conversation,
                kind=_WorkKind.RESET,
            )

        action_ids = self._new_action_ids()
        record = _ExecutionRecord(
            source=message,
            conversation_key=conversation,
            question=question,
            action_ids=action_ids,
            capabilities=self._capabilities,
        )
        self._live_records[message.execution_id] = record
        self._register_actions(record)
        try:
            record.reply_session = await self._channel.begin_reply(
                message,
                max_content_chars=self._max_answer_chars,
                context=ReplyContext(
                    status=ReplyStatus.QUEUED,
                    queue_position=queue_position,
                    actions=action_ids,
                    capabilities=self._capabilities,
                    telemetry=self._telemetry,
                    telemetry_started_at=telemetry_started_at,
                ),
            )
            record.reply_message_id = record.reply_session.reply_message_id
        except Exception:
            self._drop_actions(record)
            self._release_record(record)
            self._log(logging.ERROR, "reply_failed", message, "REPLY_BEGIN_FAILURE")
            await self._mark_executed(message)
            await self._safe_reply(message, SERVICE_UNAVAILABLE_TEXT)
            return _PreparedMessage(
                source=message,
                conversation_key=conversation,
                kind=_WorkKind.EMPTY,
                outcome=ChannelMessageOutcome(
                    ChannelResult.FAILED,
                    ChannelReason.DELIVERY_FAILURE,
                ),
            )

        if action_ids.regenerate or action_ids.retry:
            self._latest_execution_by_conversation[conversation] = message.execution_id
        else:
            self._latest_execution_by_conversation.pop(conversation, None)

        return _PreparedMessage(
            source=message,
            conversation_key=conversation,
            kind=_WorkKind.QUESTION,
            record=record,
        )

    async def _run_question(self, record: _ExecutionRecord) -> None:
        reply_session = record.reply_session
        if reply_session is None:
            return
        if record.cancel_requested:
            await self._finalize_cancel(record)
            return

        try:
            await reply_session.set_status(ReplyStatus.RUNNING)
        except Exception:
            if record.status not in _TERMINAL_REPLY_STATUSES:
                self._log(logging.ERROR, "reply_failed", record.source, "REPLY_STATUS_FAILURE")
                await self._mark_executed(record.source)
                await self._fail_reply_session(record, "REPLY_STATUS_FAILURE")
            return
        if record.status in _TERMINAL_REPLY_STATUSES:
            # A cooperative shutdown can terminalize this card while the status
            # patch is still in flight. Never resurrect a finished reply.
            return
        record.status = ReplyStatus.RUNNING
        if record.cancel_requested:
            await self._finalize_cancel(record)
            return

        started_at = time.monotonic()
        task = asyncio.create_task(
            self._consume_execution(record, started_at=started_at),
            name=f"channel-execute-{_short_hash(record.source.execution_id)}",
        )
        record.execution_task = task
        if record.cancel_requested:
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            if not record.cancel_requested:
                raise
            # The execution task and its caller can be cancelled in the same
            # event-loop turn during worker shutdown. On Python 3.12 the
            # caller then observes the child's CancelledError while its own
            # cancellation request remains recorded on the task. Finalize the
            # visible reply, but do not swallow that outer cancellation or the
            # worker consumer can continue into its next Queue.get forever.
            current_task = asyncio.current_task()
            propagate_caller_cancel = current_task is not None and current_task.cancelling() > 0
            await self._finalize_cancel(record)
            if propagate_caller_cancel:
                raise
        finally:
            record.execution_task = None
            self._telemetry.execution_duration(
                provider=self._provider,
                operation=channel_operation(record.source.operation),
                result=self._execution_result(record.status),
                seconds=max(0.0, time.monotonic() - started_at),
            )

    async def _consume_execution(
        self,
        record: _ExecutionRecord,
        *,
        started_at: float,
    ) -> None:
        reply_session = record.reply_session
        if reply_session is None:
            return
        message = record.source
        try:
            terminal_seen = False
            # ``aclosing`` is what makes a cancelled execution release its
            # private SSE response now instead of whenever the loop happens to
            # finalize an abandoned generator. Cooperative shutdown depends on
            # it: the transport must be closed before the client is torn down.
            async with contextlib.aclosing(
                self._executor.stream(
                    question=record.question,
                    event_id=message.execution_id,
                    conversation_key=record.conversation_key,
                    provider=message.channel,
                    subject=message.sender_id,
                    conversation=message.chat_id,
                    identity=message.identity,
                    operation=message.operation,
                    presentation_ref=record.reply_message_id or None,
                )
            ) as events:
                async for event in events:
                    if terminal_seen:
                        raise AgentExecutionError("CHANNEL_EXECUTION_INVALID_STREAM")
                    if isinstance(event, MessageDeltaEvent):
                        try:
                            await reply_session.append(event.content)
                        except Exception:
                            self._log(
                                logging.ERROR,
                                "reply_failed",
                                message,
                                "REPLY_APPEND_FAILURE",
                            )
                            await self._mark_executed(message)
                            await self._fail_reply_session(record, "REPLY_APPEND_FAILURE")
                            return
                        continue
                    if isinstance(event, ExecutionFailedEvent):
                        terminal_seen = True
                        error_code = f"CHANNEL_EXECUTION_{event.error_code}"
                        self._log(logging.ERROR, "execution_failed", message, error_code)
                        await self._mark_executed(message)
                        await self._fail_reply_session(record, error_code)
                        continue
                    if isinstance(event, MessageCompletedEvent):
                        terminal_seen = True
                        await self._complete_reply(
                            record,
                            session_id=event.session_id,
                            authoritative_content=event.content,
                            started_at=started_at,
                        )
                        continue
                    if isinstance(event, InteractionRequiredEvent):
                        terminal_seen = True
                        await self._pause_for_interaction(record, event)
            if terminal_seen:
                return
            raise AgentExecutionError("CHANNEL_EXECUTION_INCOMPLETE")
        except asyncio.CancelledError:
            raise
        except AgentExecutionError as exc:
            self._log(logging.ERROR, "execution_failed", message, exc.code)
            await self._mark_executed(message)
            await self._fail_reply_session(record, exc.code)
        except Exception:
            self._log(
                logging.ERROR,
                "execution_failed",
                message,
                "CHANNEL_EXECUTION_FAILURE",
            )
            await self._mark_executed(message)
            await self._fail_reply_session(record, "CHANNEL_EXECUTION_FAILURE")

    async def _pause_for_interaction(
        self,
        record: _ExecutionRecord,
        event: InteractionRequiredEvent,
    ) -> None:
        reply_session = record.reply_session
        client = self._interaction_client
        presenter = self._interaction_presenter
        if reply_session is None or not isinstance(reply_session, InteractionReplySession) or client is None or presenter is None:
            await self._mark_executed(record.source)
            await self._fail_reply_session(
                record,
                "CHANNEL_INTERACTION_UNSUPPORTED",
            )
            return
        try:
            presentation_ref = await reply_session.pause_for_interaction()
            if presentation_ref != record.reply_message_id or not presentation_ref:
                raise AgentExecutionError("CHANNEL_INTERACTION_PRESENTATION_MISMATCH")
            delivery = await client.claim_interaction_delivery(
                owner=self._interaction_owner,
                action_id=event.action_id,
                revision=event.revision,
            )
            if delivery is None or delivery.kind != "form" or delivery.action_id != event.action_id or delivery.revision != event.revision or delivery.presentation_ref != presentation_ref:
                raise AgentExecutionError("CHANNEL_INTERACTION_DELIVERY_MISMATCH")
            delivered = await self._publish_interaction_delivery(delivery)
        except asyncio.CancelledError:
            raise
        except Exception:
            record.status = ReplyStatus.ERROR
            await self._mark_executed(record.source)
            self._release_record(record)
            self._log(
                logging.ERROR,
                "interaction_pause_failed",
                record.source,
                "CHANNEL_INTERACTION_DELIVERY_FAILED",
            )
            self._interaction_wakeup.set()
            return
        record.status = ReplyStatus.AWAITING_INPUT
        if delivered:
            await self._mark_replied(record.source)
        else:
            await self._mark_executed(record.source)
        self._release_record(record)
        self._interaction_revisions[event.action_id] = event.revision
        self._interaction_wakeup.set()
        self._log(
            logging.INFO,
            "interaction_required",
            record.source,
            "",
            result="ok" if delivered else "pending_retry",
        )

    async def _poll_interaction_delivery(self) -> bool:
        client = self._interaction_client
        if client is None:
            return False
        for action_id, revision in tuple(self._interaction_revisions.items()):
            for candidate_revision in (revision, revision + 1):
                delivery = await client.claim_interaction_delivery(
                    owner=self._interaction_owner,
                    action_id=action_id,
                    revision=candidate_revision,
                )
                if delivery is None:
                    continue
                delivered = await self._publish_interaction_delivery(delivery)
                if delivered:
                    if delivery.kind == "terminal":
                        self._interaction_revisions.pop(action_id, None)
                    else:
                        self._interaction_revisions[action_id] = delivery.revision
                return True
        # With no live reply, an unscoped claim is restart/orphan recovery and
        # cannot race a still-streaming card owned by this bridge.
        if self._live_records:
            return False
        delivery = await client.claim_interaction_delivery(
            owner=self._interaction_owner,
        )
        if delivery is None:
            return False
        delivered = await self._publish_interaction_delivery(delivery)
        if delivered and delivery.kind == "form":
            self._interaction_revisions[delivery.action_id] = delivery.revision
        return True

    async def _publish_interaction_delivery(
        self,
        delivery: ClaimedInteractionDelivery,
    ) -> bool:
        client = self._interaction_client
        presenter = self._interaction_presenter
        if client is None or presenter is None:
            return False
        try:
            await presenter.present(delivery)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._telemetry.delivery(
                provider=self._provider,
                operation=ChannelOperation.OTHER,
                stage=ChannelStage.DELIVERY,
                result=ChannelResult.FAILED,
                reason=ChannelReason.DELIVERY_FAILURE,
            )
            LOGGER.warning(
                "channel_event=interaction_render result=failed error_code=CHANNEL_INTERACTION_RENDER_FAILED error_type=%s",
                type(exc).__name__,
            )
            try:
                await client.acknowledge_interaction_delivery(
                    delivery=delivery,
                    owner=self._interaction_owner,
                    success=False,
                    safe_error_code="CHANNEL_INTERACTION_RENDER_FAILED",
                )
            except Exception:
                LOGGER.warning(
                    "channel_event=interaction_delivery_ack result=failed error_code=CHANNEL_INTERACTION_ACK_FAILED",
                )
            return False
        try:
            await client.acknowledge_interaction_delivery(
                delivery=delivery,
                owner=self._interaction_owner,
                success=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._telemetry.delivery(
                provider=self._provider,
                operation=ChannelOperation.OTHER,
                stage=ChannelStage.DELIVERY,
                result=ChannelResult.FAILED,
                reason=ChannelReason.STATE_FAILURE,
            )
            LOGGER.warning(
                "channel_event=interaction_delivery_ack result=failed error_code=CHANNEL_INTERACTION_ACK_FAILED",
            )
            return False
        self._telemetry.delivery(
            provider=self._provider,
            operation=ChannelOperation.OTHER,
            stage=ChannelStage.DELIVERY,
            result=ChannelResult.OK,
            reason=ChannelReason.NONE,
        )
        return True

    async def _complete_reply(
        self,
        record: _ExecutionRecord,
        *,
        session_id: str,
        authoritative_content: str | None,
        started_at: float,
    ) -> None:
        message = record.source
        elapsed_ms = round((time.monotonic() - started_at) * 1000)
        reply_session = record.reply_session
        if reply_session is None:
            self._release_record(record)
            return
        # Receiving the trusted terminal event is the cancellation barrier.
        # Delivery may still fail and downgrade to ERROR below, but neither a
        # late stop callback nor a cooperative shutdown may cancel final card
        # replacement mid-flight.
        record.status = ReplyStatus.FINAL
        try:
            if authoritative_content is not None:
                await reply_session.replace(authoritative_content)
            await reply_session.complete()
        except Exception:
            record.telemetry_outcome = ChannelMessageOutcome(
                ChannelResult.FAILED,
                ChannelReason.DELIVERY_FAILURE,
            )
            self._log(logging.ERROR, "reply_failed", message, "REPLY_DELIVERY_FAILURE")
            await self._mark_executed(message)
            if reply_session.state is ReplySessionState.OPEN:
                await self._fail_reply_session(record, "REPLY_DELIVERY_FAILURE")
            elif reply_session.state is ReplySessionState.COMPLETED:
                record.status = ReplyStatus.FINAL
            else:
                record.status = ReplyStatus.ERROR
            self._release_record(record)
            return
        try:
            await self._state_store.mark_replied(message.execution_id)
        except Exception:
            record.telemetry_outcome = ChannelMessageOutcome(
                ChannelResult.FAILED,
                ChannelReason.STATE_FAILURE,
            )
            self._log(logging.ERROR, "state_failed", message, "REDIS_MARK_REPLIED")
            await self._mark_executed(message)
            self._release_record(record)
            return
        record.telemetry_outcome = ChannelMessageOutcome(ChannelResult.OK)
        self._release_record(record)
        self._log(
            logging.INFO,
            "execution_completed",
            message,
            "",
            result="ok",
            execution_ms=elapsed_ms,
            session_id=session_id,
        )

    async def _fail_reply_session(
        self,
        record: _ExecutionRecord,
        error_code: str,
    ) -> None:
        reply_session = record.reply_session
        if reply_session is None:
            self._release_record(record)
            return
        record.status = ReplyStatus.ERROR
        try:
            await reply_session.fail(error_code)
        except Exception:
            self._log(logging.ERROR, "reply_failed", record.source, "REPLY_FAILED")
        self._release_record(record)

    async def _finalize_cancel(
        self,
        record: _ExecutionRecord,
    ) -> ChannelMessageOutcome:
        if record.status in _TERMINAL_REPLY_STATUSES:
            return self._message_outcome(record)
        if record.finalizing:
            await record.finalization_done.wait()
            if record.finalization_outcome is not None:
                return record.finalization_outcome
            # The owner itself was cancelled before publishing an outcome.
            # Take ownership and retry instead of letting a waiter infer OK.
            return await self._finalize_cancel(record)
        reply_session = record.reply_session
        if reply_session is None:
            outcome = ChannelMessageOutcome(
                ChannelResult.FAILED,
                ChannelReason.DELIVERY_FAILURE,
            )
            record.status = ReplyStatus.ERROR
            record.telemetry_outcome = outcome
            record.finalization_outcome = outcome
            self._release_record(record)
            return outcome
        record.finalizing = True
        record.finalization_done.clear()
        outcome: ChannelMessageOutcome | None = None
        try:
            if reply_session.state is ReplySessionState.OPEN:
                try:
                    await reply_session.cancel()
                except Exception:
                    self._log(
                        logging.ERROR,
                        "reply_failed",
                        record.source,
                        "REPLY_CANCEL_FAILURE",
                    )
                    await self._mark_executed(record.source)
                    record.status = ReplyStatus.ERROR
                    outcome = ChannelMessageOutcome(
                        ChannelResult.FAILED,
                        ChannelReason.DELIVERY_FAILURE,
                    )
                else:
                    try:
                        await self._state_store.mark_replied(record.source.execution_id)
                    except Exception:
                        self._log(
                            logging.ERROR,
                            "state_failed",
                            record.source,
                            "REDIS_MARK_REPLIED",
                        )
                        await self._mark_executed(record.source)
                        record.status = ReplyStatus.CANCELLED
                        outcome = ChannelMessageOutcome(
                            ChannelResult.FAILED,
                            ChannelReason.STATE_FAILURE,
                        )
            if outcome is None:
                record.status = ReplyStatus.CANCELLED
                outcome = ChannelMessageOutcome(
                    ChannelResult.CANCELLED,
                    record.telemetry_reason,
                )
            record.telemetry_outcome = outcome
            record.finalization_outcome = outcome
            self._release_record(record)
        except asyncio.CancelledError:
            reason = ChannelReason.SHUTDOWN_TIMEOUT if record.telemetry_reason is ChannelReason.SHUTDOWN else ChannelReason.DELIVERY_FAILURE
            outcome = ChannelMessageOutcome(ChannelResult.FAILED, reason)
            record.status = ReplyStatus.ERROR
            record.telemetry_outcome = outcome
            record.finalization_outcome = outcome
            self._telemetry.delivery(
                provider=self._provider,
                operation=channel_operation(record.source.operation),
                stage=ChannelStage.DELIVERY,
                result=ChannelResult.FAILED,
                reason=reason,
            )
            self._release_record(record)
            raise
        finally:
            record.finalizing = False
            record.finalization_done.set()
        if outcome.result is ChannelResult.CANCELLED:
            self._log(
                logging.INFO,
                "execution_cancelled",
                record.source,
                "",
                result="ok",
            )
        return outcome

    async def _run_reset(self, prepared: _PreparedMessage) -> ChannelMessageOutcome:
        started_at = time.monotonic()
        try:
            await self._executor.reset(conversation_key=prepared.conversation_key)
        except Exception:
            await self._mark_executed(prepared.source)
            await self._safe_reply(prepared.source, SERVICE_UNAVAILABLE_TEXT)
            outcome = ChannelMessageOutcome(
                ChannelResult.FAILED,
                ChannelReason.EXECUTION_FAILURE,
            )
            self._telemetry.execution_duration(
                provider=self._provider,
                operation=channel_operation(prepared.source.operation),
                result=outcome.result,
                seconds=max(0.0, time.monotonic() - started_at),
            )
            return outcome
        self._latest_execution_by_conversation.pop(prepared.conversation_key, None)
        outcome = await self._reply_and_complete(prepared.source, SESSION_RESET_TEXT)
        self._telemetry.execution_duration(
            provider=self._provider,
            operation=channel_operation(prepared.source.operation),
            result=outcome.result,
            seconds=max(0.0, time.monotonic() - started_at),
        )
        return outcome

    async def _reply_queue_rejected(
        self,
        message: IncomingMessage,
        *,
        reason: str,
    ) -> None:
        try:
            claimed = await self._state_store.claim_message(message.execution_id)
        except Exception:
            self._log(logging.ERROR, "state_failed", message, "REDIS_CLAIM_FAILED")
            return
        if not claimed:
            return
        await self._reply_and_complete(message, QUEUE_BUSY_TEXT)
        self._log(logging.INFO, "queue_rejected", message, reason, result="rejected")

    async def _acknowledge_feedback(
        self,
        record: _ExecutionRecord,
        *,
        helpful: bool,
    ) -> None:
        reply_session = record.reply_session
        if reply_session is not None:
            await reply_session.acknowledge_feedback(helpful=helpful)
        LOGGER.info(
            "channel_event=feedback_recorded binding_id_hash=%s message_id_hash=%s helpful=%s result=ok error_code=",
            _short_hash(self._binding_id),
            _short_hash(record.source.execution_id),
            str(helpful).lower(),
        )

    def _new_action_ids(self) -> ReplyActionIds:
        return ReplyActionIds(
            cancel=secrets.token_urlsafe(24) if self._capabilities.cancel_queued or self._capabilities.cancel_running else "",
            regenerate=secrets.token_urlsafe(24) if self._capabilities.regenerate else "",
            retry=secrets.token_urlsafe(24) if self._capabilities.retry else "",
            helpful=secrets.token_urlsafe(24) if self._capabilities.feedback else "",
            unhelpful=secrets.token_urlsafe(24) if self._capabilities.feedback else "",
        )

    def _register_actions(self, record: _ExecutionRecord) -> None:
        self._cleanup_actions()
        expires_at = time.monotonic() + _ACTION_TTL_SECONDS
        for kind, action_id in (
            (ReplyActionKind.CANCEL, record.action_ids.cancel),
            (ReplyActionKind.REGENERATE, record.action_ids.regenerate),
            (ReplyActionKind.RETRY, record.action_ids.retry),
            (ReplyActionKind.HELPFUL, record.action_ids.helpful),
            (ReplyActionKind.UNHELPFUL, record.action_ids.unhelpful),
        ):
            if not action_id:
                continue
            self._actions[action_id] = _RegisteredAction(
                record=record,
                kind=kind,
                expires_at=expires_at,
            )

    def _cleanup_actions(self) -> None:
        now = time.monotonic()
        stale = [action_id for action_id, registered in self._actions.items() if registered.expires_at <= now]
        for action_id in stale:
            self._actions.pop(action_id, None)
        overflow = len(self._actions) - _MAX_ACTIONS
        if overflow > 0:
            oldest = sorted(
                self._actions.items(),
                key=lambda item: item[1].expires_at,
            )[:overflow]
            for action_id, _registered in oldest:
                self._actions.pop(action_id, None)
        live_execution_ids = {registered.record.source.execution_id for registered in self._actions.values()}
        stale_conversations = [conversation for conversation, execution_id in self._latest_execution_by_conversation.items() if execution_id not in live_execution_ids]
        for conversation in stale_conversations:
            self._latest_execution_by_conversation.pop(conversation, None)

    def _release_record(self, record: _ExecutionRecord) -> None:
        """Drop a reply whose card can no longer be changed by this process."""

        if self._live_records.get(record.source.execution_id) is record:
            del self._live_records[record.source.execution_id]

    @staticmethod
    def _execution_result(status: ReplyStatus) -> ChannelResult:
        if status is ReplyStatus.FINAL:
            return ChannelResult.OK
        if status is ReplyStatus.AWAITING_INPUT:
            return ChannelResult.AWAITING_INPUT
        if status is ReplyStatus.CANCELLED:
            return ChannelResult.CANCELLED
        return ChannelResult.FAILED

    @classmethod
    def _message_outcome(cls, record: _ExecutionRecord) -> ChannelMessageOutcome:
        if record.telemetry_outcome is not None:
            return record.telemetry_outcome
        result = cls._execution_result(record.status)
        if result is ChannelResult.FAILED:
            return ChannelMessageOutcome(result, ChannelReason.EXECUTION_FAILURE)
        return ChannelMessageOutcome(result, record.telemetry_reason)

    def _drop_actions(self, record: _ExecutionRecord) -> None:
        for action_id in (
            record.action_ids.cancel,
            record.action_ids.regenerate,
            record.action_ids.retry,
            record.action_ids.helpful,
            record.action_ids.unhelpful,
        ):
            self._actions.pop(action_id, None)

    def _mark_feedback_actions_used(self, record: _ExecutionRecord) -> None:
        for action_id in (record.action_ids.helpful, record.action_ids.unhelpful):
            registered = self._actions.get(action_id)
            if registered is not None:
                registered.used = True

    @staticmethod
    def _regenerated_message(
        source: IncomingMessage,
        action: ChannelAction,
        *,
        replace_completed: bool,
    ) -> IncomingMessage:
        return IncomingMessage(
            channel=source.channel,
            account_id=source.account_id,
            chat_id=source.chat_id,
            chat_type=source.chat_type,
            message_id=source.message_id,
            sender_id=source.sender_id,
            content=source.content,
            identity=source.identity,
            message_type=source.message_type,
            sender_type=source.sender_type,
            event_id=action.event_id,
            request_id=f"action:{action.event_id}",
            operation="regenerate" if replace_completed else source.operation,
        )

    def _spawn_background(
        self,
        coroutine: Awaitable[None],
        *,
        name: str,
    ) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_task_done)

    def _background_task_done(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            LOGGER.error(
                "channel_event=background_action_failed binding_id_hash=%s result=failed error_code=BACKGROUND_ACTION_FAILURE",
                _short_hash(self._binding_id),
            )

    async def _reply_and_complete(
        self,
        message: IncomingMessage,
        content: str,
    ) -> ChannelMessageOutcome:
        try:
            await self._channel.send(
                OutgoingMessage(
                    chat_id=message.chat_id,
                    content=content,
                    reply_to_message_id=message.message_id,
                )
            )
        except Exception:
            self._log(logging.ERROR, "reply_failed", message, "REPLY_OR_STATE_FAILURE")
            await self._mark_executed(message)
            self._telemetry.delivery(
                provider=self._provider,
                operation=channel_operation(message.operation),
                stage=ChannelStage.DELIVERY,
                result=ChannelResult.FAILED,
                reason=ChannelReason.DELIVERY_FAILURE,
            )
            return ChannelMessageOutcome(
                ChannelResult.FAILED,
                ChannelReason.DELIVERY_FAILURE,
            )
        self._telemetry.delivery(
            provider=self._provider,
            operation=channel_operation(message.operation),
            stage=ChannelStage.DELIVERY,
            result=ChannelResult.OK,
            reason=ChannelReason.NONE,
        )
        try:
            await self._state_store.mark_replied(message.execution_id)
        except Exception:
            self._log(logging.ERROR, "state_failed", message, "REDIS_MARK_REPLIED")
            await self._mark_executed(message)
            return ChannelMessageOutcome(
                ChannelResult.FAILED,
                ChannelReason.STATE_FAILURE,
            )
        return ChannelMessageOutcome(ChannelResult.OK)

    async def _safe_reply(self, message: IncomingMessage, content: str) -> None:
        try:
            await self._channel.send(
                OutgoingMessage(
                    chat_id=message.chat_id,
                    content=content,
                    reply_to_message_id=message.message_id,
                )
            )
        except Exception:
            self._log(logging.ERROR, "reply_failed", message, "REPLY_FAILED")
            self._telemetry.delivery(
                provider=self._provider,
                operation=channel_operation(message.operation),
                stage=ChannelStage.DELIVERY,
                result=ChannelResult.FAILED,
                reason=ChannelReason.DELIVERY_FAILURE,
            )
            return
        self._telemetry.delivery(
            provider=self._provider,
            operation=channel_operation(message.operation),
            stage=ChannelStage.DELIVERY,
            result=ChannelResult.FALLBACK,
            reason=ChannelReason.NONE,
        )

    async def _mark_replied(self, message: IncomingMessage) -> ChannelMessageOutcome:
        try:
            await self._state_store.mark_replied(message.execution_id)
        except Exception:
            self._log(logging.ERROR, "state_failed", message, "REDIS_MARK_REPLIED")
            return ChannelMessageOutcome(
                ChannelResult.FAILED,
                ChannelReason.STATE_FAILURE,
            )
        return ChannelMessageOutcome(ChannelResult.OK)

    async def _mark_executed(self, message: IncomingMessage) -> None:
        try:
            await self._state_store.mark_executed(message.execution_id)
        except Exception:
            self._log(logging.ERROR, "state_failed", message, "REDIS_MARK_EXECUTED")

    def _log(
        self,
        level: int,
        event: str,
        message: IncomingMessage,
        error_code: str,
        *,
        result: str = "failed",
        execution_ms: int | None = None,
        session_id: str = "",
    ) -> None:
        LOGGER.log(
            level,
            "channel_event=%s binding_id_hash=%s message_id_hash=%s sender_id_hash=%s chat_id_hash=%s session_id_hash=%s execution_ms=%s result=%s error_code=%s",
            event,
            _short_hash(self._binding_id),
            _short_hash(message.execution_id),
            _short_hash(message.sender_id),
            _short_hash(message.chat_id),
            _short_hash(session_id) if session_id else "",
            execution_ms if execution_ms is not None else "",
            result,
            error_code,
        )
