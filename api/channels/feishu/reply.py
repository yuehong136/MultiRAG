"""Feishu progressive reply lifecycle and user-visible renderers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from api.channel_capabilities import EffectiveReplyCapabilities
from api.channels.core.base import (
    IncomingMessage,
    ReplyActionIds,
    ReplyContext,
    ReplySessionState,
    ReplySessionStateError,
    ReplyStatus,
)
from api.channels.core.reply import SERVICE_UNAVAILABLE_TEXT, strip_reasoning, truncate_answer
from api.channels.reply_session import CANCELLED_TEXT

LOGGER = logging.getLogger(__name__)

CARD_ANSWER_ELEMENT_ID = "answer"
CARD_STATUS_ELEMENT_ID = "status"
CARD_ACTIONS_ELEMENT_ID = "actions"
_CARD_CANCEL_BUTTON_ID = "reply_cancel"
_CARD_REGENERATE_BUTTON_ID = "reply_regenerate"
_CARD_RETRY_BUTTON_ID = "reply_retry"
_CARD_HELPFUL_BUTTON_ID = "reply_helpful"
_CARD_UNHELPFUL_BUTTON_ID = "reply_unhelpful"
# CardKit needs the answer element to exist before streaming updates begin.
# Keep a non-empty but invisible placeholder so the lifecycle status remains
# the single user-visible progress indicator until the first answer token.
_CARD_ANSWER_PLACEHOLDER = " "
_CARD_BYTE_LIMIT = 24_000
_CARD_PRINT_FREQUENCY_MS = 70
_CARD_PRINT_STEP = 1
_DELIVERY_UUID_LENGTH = 40
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+[^)]*)?\)")
_MASS_MENTION_RE = re.compile(r"(?i)@(all|everyone)\b")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")


@runtime_checkable
class FeishuReplyTransport(Protocol):
    """Provider operations used by the progressive session."""

    async def add_typing_reaction(self, message_id: str) -> str: ...

    async def remove_reaction(self, message_id: str, reaction_id: str) -> None: ...

    async def create_streaming_card(self, card_json: str) -> str: ...

    async def reply_card(
        self,
        message_id: str,
        card_id: str,
        *,
        delivery_uuid: str,
    ) -> str: ...

    async def update_card_text(
        self,
        card_id: str,
        content: str,
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> None: ...

    async def batch_update_card(
        self,
        card_id: str,
        actions: list[dict[str, object]],
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> None: ...

    async def finish_streaming_card(
        self,
        card_id: str,
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> None: ...

    async def reply_content(
        self,
        message_id: str,
        content: str,
        *,
        message_type: str,
        delivery_uuid: str,
    ) -> None: ...


def delivery_uuid(source: IncomingMessage, stage: str) -> str:
    """Return a deterministic, opaque idempotency key for one delivery stage."""

    event_id = source.event_id or source.message_id
    material = f"{source.account_id}\0{event_id}\0{stage}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:_DELIVERY_UUID_LENGTH]


def render_markdown(content: str) -> str:
    """Render model Markdown into the safe CardKit Markdown subset."""

    safe = strip_reasoning(content)
    safe = _IMAGE_RE.sub(lambda match: match.group(1), safe)
    safe = _LINK_RE.sub(_safe_markdown_link, safe)
    safe = _MASS_MENTION_RE.sub(lambda match: f"＠{match.group(1)}", safe)
    safe = safe.replace("<", "&lt;").replace(">", "&gt;")
    safe = _fence_tables(safe)
    if _has_unclosed_fence(safe):
        safe += "\n```"
    return safe.strip()


def render_text(content: str, *, max_chars: int) -> str:
    """Render safe plain text for the terminal compatibility fallback."""

    if max_chars < 1:
        raise ValueError("reply content limit must be positive")
    markdown = render_markdown(content)
    text = _LINK_RE.sub(lambda match: f"{match.group(1)} ({match.group(2)})", markdown)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^\s*```[^\n]*$", "", text)
    text = re.sub(r"(?<!\\)(\*\*|__|~~|`)", "", text)
    text = text.replace("&lt;", "＜").replace("&gt;", "＞").strip()
    return truncate_answer(text, max_chars)


def render_post(content: str, *, max_chars: int) -> str:
    """Render a conservative Feishu rich-text post without active mentions."""

    text = render_text(content, max_chars=max_chars)
    paragraphs = text.splitlines() or [text]
    payload = {
        "zh_cn": {
            # The application name is already visible beside every reply. Keep
            # the compatibility fallback unbranded so a transport downgrade
            # does not look like model-authored content.
            "title": "",
            "content": [[{"tag": "text", "text": paragraph or " "}] for paragraph in paragraphs],
        }
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _status_text(status: ReplyStatus, *, queue_position: int = 0) -> str:
    if status is ReplyStatus.QUEUED:
        if queue_position > 0:
            return f"⏳ 已排队 · 前面还有 {queue_position} 条"
        return "⏳ 已接收 · 等待开始"
    if status is ReplyStatus.RUNNING:
        return "✨ 正在生成"
    if status is ReplyStatus.FINAL:
        return "✅ 回答完成"
    if status is ReplyStatus.CANCELLED:
        return "⏹️ 已停止生成 · 外部操作不视为已撤销"
    return "⚠️ 生成失败"


def _summary_text(status: ReplyStatus) -> str:
    if status is ReplyStatus.FINAL:
        return "回答完成"
    if status is ReplyStatus.CANCELLED:
        return "已停止生成"
    if status is ReplyStatus.ERROR:
        return "生成失败"
    if status is ReplyStatus.QUEUED:
        return "[排队中]"
    return "[生成中]"


def _button(
    label: str,
    action_id: str,
    *,
    element_id: str,
    button_type: str = "default",
) -> dict[str, object]:
    return {
        "tag": "button",
        "element_id": element_id,
        "text": {"tag": "plain_text", "content": label},
        "type": button_type,
        "size": "medium",
        "width": "default",
        # The provider callback carries only this opaque value. Identity,
        # question text and execution parameters stay in server-owned state.
        "behaviors": [
            {
                "type": "callback",
                "value": {"action_id": action_id},
            }
        ],
    }


def _action_element(
    status: ReplyStatus,
    actions: ReplyActionIds,
    capabilities: EffectiveReplyCapabilities,
    *,
    feedback: bool | None = None,
) -> dict[str, object]:
    if feedback is not None:
        content = "感谢反馈：这个回答有帮助。" if feedback else "感谢反馈：我们会继续改进。"
        return {
            "tag": "markdown",
            "element_id": CARD_ACTIONS_ELEMENT_ID,
            "content": content,
        }

    buttons: list[dict[str, object]] = []
    can_cancel = capabilities.cancel_queued if status is ReplyStatus.QUEUED else capabilities.cancel_running
    if status in {ReplyStatus.QUEUED, ReplyStatus.RUNNING} and can_cancel and actions.cancel:
        buttons.append(
            _button(
                "停止生成",
                actions.cancel,
                element_id=_CARD_CANCEL_BUTTON_ID,
            )
        )
    elif status is ReplyStatus.FINAL:
        if capabilities.regenerate and actions.regenerate:
            buttons.append(
                _button(
                    "重新生成",
                    actions.regenerate,
                    element_id=_CARD_REGENERATE_BUTTON_ID,
                )
            )
        if capabilities.feedback and actions.helpful:
            buttons.append(
                _button(
                    "有帮助",
                    actions.helpful,
                    element_id=_CARD_HELPFUL_BUTTON_ID,
                    button_type="primary",
                )
            )
        if capabilities.feedback and actions.unhelpful:
            buttons.append(
                _button(
                    "没帮助",
                    actions.unhelpful,
                    element_id=_CARD_UNHELPFUL_BUTTON_ID,
                )
            )
    elif status in {ReplyStatus.ERROR, ReplyStatus.CANCELLED} and capabilities.retry and actions.retry:
        buttons.append(
            _button(
                "重试",
                actions.retry,
                element_id=_CARD_RETRY_BUTTON_ID,
            )
        )

    if not buttons:
        return {
            "tag": "markdown",
            "element_id": CARD_ACTIONS_ELEMENT_ID,
            "content": " ",
        }
    # Card JSON 2.0 removed the legacy ``tag: action`` module. A column set is
    # the documented horizontal layout for direct button components and can be
    # atomically replaced through CardKit's ``update_element`` operation.
    return {
        "tag": "column_set",
        "element_id": CARD_ACTIONS_ELEMENT_ID,
        "flex_mode": "none",
        "background_style": "default",
        "horizontal_spacing": "default",
        "margin": "0px",
        "columns": [
            {
                "tag": "column",
                "width": "auto",
                "vertical_align": "top",
                "elements": [button],
            }
            for button in buttons
        ],
    }


def streaming_card_json(context: ReplyContext | None = None) -> str:
    """Build one CardKit JSON 2.0 shell for the full reply lifecycle."""

    initial = context or ReplyContext()

    card = {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "streaming_mode": True,
            "streaming_config": {
                "print_step": {"default": _CARD_PRINT_STEP},
                "print_frequency_ms": {"default": _CARD_PRINT_FREQUENCY_MS},
                "print_strategy": "fast",
            },
            "summary": {"content": _summary_text(initial.status)},
        },
        "body": {
            "direction": "vertical",
            "elements": [
                {
                    "tag": "markdown",
                    "element_id": CARD_STATUS_ELEMENT_ID,
                    "content": _status_text(
                        initial.status,
                        queue_position=initial.queue_position,
                    ),
                },
                {
                    "tag": "markdown",
                    "element_id": CARD_ANSWER_ELEMENT_ID,
                    "content": _CARD_ANSWER_PLACEHOLDER,
                },
                _action_element(initial.status, initial.actions, initial.capabilities),
            ],
        },
    }
    return json.dumps(card, ensure_ascii=False, separators=(",", ":"))


class FeishuProgressiveReplySession:
    """Coalesce deltas into one CardKit stream with post/text fallback."""

    def __init__(
        self,
        *,
        transport: FeishuReplyTransport,
        source: IncomingMessage,
        max_content_chars: int,
        context: ReplyContext = ReplyContext(),
        update_interval_seconds: float = 0.25,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_content_chars < 1:
            raise ValueError("reply content limit must be positive")
        if update_interval_seconds <= 0:
            raise ValueError("update interval must be positive")
        self._transport = transport
        self._source = source
        self._max_content_chars = max_content_chars
        self._reply_status = context.status
        self._queue_position = context.queue_position
        self._action_ids = context.actions
        self._capabilities = context.capabilities
        self._update_interval_seconds = update_interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._started_at = clock()
        self._parts: list[str] = []
        self._state = ReplySessionState.OPEN
        self._reaction_id = ""
        self._reaction_task: asyncio.Task[None] | None = None
        self._card_id = ""
        self._reply_message_id = ""
        self._card_visible = False
        self._card_active = False
        self._sequence = 0
        self._last_rendered = ""
        self._flush_task: asyncio.Task[None] | None = None
        self._flush_in_flight = False
        self._terminal_flush_requested = False

    @classmethod
    async def begin(
        cls,
        *,
        transport: FeishuReplyTransport,
        source: IncomingMessage,
        max_content_chars: int,
        context: ReplyContext | None = None,
        update_interval_seconds: float = 0.25,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> FeishuProgressiveReplySession:
        session = cls(
            transport=transport,
            source=source,
            max_content_chars=max_content_chars,
            context=context or ReplyContext(),
            update_interval_seconds=update_interval_seconds,
            clock=clock,
            sleep=sleep,
        )
        await session._start_progression()
        return session

    @property
    def state(self) -> ReplySessionState:
        return self._state

    @property
    def reply_message_id(self) -> str:
        return self._reply_message_id

    async def append(self, content: str) -> None:
        self._require_open("append")
        if not isinstance(content, str):
            raise TypeError("reply delta must be a string")
        self._parts.append(content)
        if not content or not self._card_active:
            return
        self._schedule_card_update()

    async def set_status(
        self,
        status: ReplyStatus,
        *,
        queue_position: int = 0,
    ) -> None:
        self._require_open("set status")
        if status not in {ReplyStatus.QUEUED, ReplyStatus.RUNNING}:
            raise ValueError("only non-terminal reply status can be set directly")
        if status is self._reply_status:
            return
        self._reply_status = status
        self._queue_position = queue_position
        if status is ReplyStatus.RUNNING and self._reaction_task is None:
            self._reaction_task = asyncio.create_task(self._add_typing())
        if self._card_active:
            await self._patch_lifecycle(status, queue_position=queue_position)

    async def complete(self) -> None:
        self._require_open("complete")
        answer = strip_reasoning("".join(self._parts))
        self._state = ReplySessionState.COMPLETED
        self._terminal_flush_requested = True

        try:
            await self._drain_card_update()
            if not answer:
                raise ReplySessionStateError("cannot complete an empty reply")
            if not self._card_active:
                await self._fallback(answer, stage="final_fallback")
                return
            try:
                await self._patch_answer(answer=answer)
            except Exception:
                self._card_active = False
                self._log("card_update_failed", "FEISHU_CARD_UPDATE_FAILED")
                await self._fallback(answer, stage="final_fallback")
                return

            try:
                await self._finish_card()
            except Exception:
                # The complete answer is already visible. A second text reply
                # would duplicate delivery, so leave the card to its platform
                # auto-close window and report only a safe operational signal.
                self._log("card_finish_failed", "FEISHU_CARD_FINISH_FAILED")
                return
            try:
                await self._patch_lifecycle(ReplyStatus.FINAL)
            except Exception:
                self._log("card_controls_failed", "FEISHU_CARD_CONTROLS_FAILED")
        finally:
            await self._remove_typing()

    async def fail(self, error_code: str) -> None:
        self._require_open("fail")
        if not error_code:
            raise ValueError("reply failure code must not be empty")
        self._state = ReplySessionState.FAILED
        self._terminal_flush_requested = True

        try:
            await self._drain_card_update()
            self._parts.clear()
            if self._card_active:
                try:
                    await self._patch_answer(answer=SERVICE_UNAVAILABLE_TEXT)
                    await self._finish_card()
                    await self._patch_lifecycle(ReplyStatus.ERROR)
                    return
                except Exception:
                    self._card_active = False
                    self._log("card_failure_render_failed", "FEISHU_CARD_FAILURE_RENDER_FAILED")
            await self._fallback(SERVICE_UNAVAILABLE_TEXT, stage="error")
        finally:
            await self._remove_typing()

    async def cancel(self) -> None:
        self._require_open("cancel")
        self._state = ReplySessionState.CANCELLED
        self._reply_status = ReplyStatus.CANCELLED
        self._terminal_flush_requested = True

        try:
            await self._drain_card_update()
            self._parts.clear()
            if self._card_active:
                try:
                    await self._patch_answer(answer=CANCELLED_TEXT)
                    await self._finish_card()
                    await self._patch_lifecycle(ReplyStatus.CANCELLED)
                    return
                except Exception:
                    self._card_active = False
                    self._log("card_cancel_render_failed", "FEISHU_CARD_CANCEL_RENDER_FAILED")
            await self._fallback(CANCELLED_TEXT, stage="cancelled")
        finally:
            await self._remove_typing()

    async def acknowledge_feedback(self, *, helpful: bool) -> None:
        if self._state is not ReplySessionState.COMPLETED or not self._card_id:
            return
        try:
            await self._patch_lifecycle(ReplyStatus.FINAL, feedback=helpful)
        except Exception:
            self._log("card_feedback_failed", "FEISHU_CARD_FEEDBACK_FAILED")

    async def _start_progression(self) -> None:
        # Reaction and card creation start concurrently. A slow best-effort ack
        # must never hold the first card behind it. The task catches all of its
        # own failures and removes a reaction that arrives after the reply has
        # already reached a terminal state.
        if self._reply_status is ReplyStatus.RUNNING:
            self._reaction_task = asyncio.create_task(self._add_typing())
            await asyncio.sleep(0)

        try:
            self._card_id = await self._transport.create_streaming_card(
                streaming_card_json(
                    ReplyContext(
                        status=self._reply_status,
                        queue_position=self._queue_position,
                        actions=self._action_ids,
                        capabilities=self._capabilities,
                    )
                )
            )
            self._reply_message_id = await self._transport.reply_card(
                self._source.message_id,
                self._card_id,
                delivery_uuid=delivery_uuid(self._source, "running_card"),
            )
            self._card_visible = True
            self._card_active = True
            self._log_latency("streaming_card_created", self._started_at)
        except Exception:
            self._card_active = False
            self._log("card_create_failed", "FEISHU_CARD_CREATE_FAILED")

    async def _add_typing(self) -> None:
        try:
            reaction_id = await self._transport.add_typing_reaction(self._source.message_id)
            self._log_latency("typing_added", self._started_at)
        except Exception:
            self._log("typing_add_failed", "FEISHU_TYPING_ADD_FAILED")
            return
        if self._state is ReplySessionState.OPEN:
            self._reaction_id = reaction_id
            return
        try:
            await self._transport.remove_reaction(self._source.message_id, reaction_id)
        except Exception:
            self._log("typing_remove_failed", "FEISHU_TYPING_REMOVE_FAILED")

    async def _patch_answer(
        self,
        *,
        answer: str | None = None,
    ) -> None:
        if not self._card_active or not self._card_id:
            return
        rendered = _truncate_utf8(render_markdown(answer if answer is not None else "".join(self._parts)))
        if not rendered:
            return
        if rendered == self._last_rendered:
            return
        sequence = self._next_sequence()
        await self._transport.update_card_text(
            self._card_id,
            rendered,
            sequence=sequence,
            delivery_uuid=delivery_uuid(self._source, f"card_update:{sequence}"),
        )
        self._last_rendered = rendered

    async def _patch_lifecycle(
        self,
        status: ReplyStatus,
        *,
        queue_position: int = 0,
        feedback: bool | None = None,
    ) -> None:
        if not self._card_id:
            return
        status_element = {
            "tag": "markdown",
            "element_id": CARD_STATUS_ELEMENT_ID,
            "content": _status_text(status, queue_position=queue_position),
        }
        actions = [
            {
                "action": "partial_update_setting",
                "params": {
                    "settings": {
                        "config": {
                            "summary": {"content": _summary_text(status)},
                        }
                    }
                },
            },
            {
                "action": "update_element",
                "params": {
                    "element_id": CARD_STATUS_ELEMENT_ID,
                    "element": status_element,
                },
            },
            {
                "action": "update_element",
                "params": {
                    "element_id": CARD_ACTIONS_ELEMENT_ID,
                    "element": _action_element(
                        status,
                        self._action_ids,
                        self._capabilities,
                        feedback=feedback,
                    ),
                },
            },
        ]
        sequence = self._next_sequence()
        await self._transport.batch_update_card(
            self._card_id,
            actions,
            sequence=sequence,
            delivery_uuid=delivery_uuid(self._source, f"card_lifecycle:{sequence}"),
        )

    def _schedule_card_update(self) -> None:
        if self._terminal_flush_requested:
            return
        task = self._flush_task
        if task is None or task.done():
            self._flush_task = asyncio.create_task(self._flush_latest())

    async def _flush_latest(self) -> None:
        try:
            await self._sleep(self._update_interval_seconds)
            while self._card_active and not self._terminal_flush_requested:
                self._flush_in_flight = True
                try:
                    await self._patch_answer()
                finally:
                    self._flush_in_flight = False

                if self._terminal_flush_requested or self._rendered_answer() == self._last_rendered:
                    return
                await self._sleep(self._update_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A renderer/transport failure is a delivery-mode failure, not an
            # Agent failure. Keep buffering subsequent deltas and deliver the
            # one final answer through the post/text fallback on completion.
            self._card_active = False
            self._log("card_update_failed", "FEISHU_CARD_UPDATE_FAILED")
        finally:
            self._flush_in_flight = False

    async def _drain_card_update(self) -> None:
        task = self._flush_task
        if task is None:
            return
        cancelled_pending = False
        if not task.done() and not self._flush_in_flight:
            task.cancel()
            cancelled_pending = True
        try:
            if cancelled_pending:
                await task
            else:
                await asyncio.shield(task)
        except asyncio.CancelledError:
            if not cancelled_pending:
                raise
        self._flush_task = None

    def _rendered_answer(self) -> str:
        return _truncate_utf8(render_markdown("".join(self._parts)))

    async def _finish_card(self) -> None:
        if not self._card_id:
            return
        sequence = self._next_sequence()
        await self._transport.finish_streaming_card(
            self._card_id,
            sequence=sequence,
            delivery_uuid=delivery_uuid(self._source, f"card_finish:{sequence}"),
        )
        self._card_active = False

    async def _fallback(self, answer: str, *, stage: str) -> None:
        fallback_uuid = delivery_uuid(self._source, stage)
        post = render_post(answer, max_chars=self._max_content_chars)
        try:
            await self._transport.reply_content(
                self._source.message_id,
                post,
                message_type="post",
                delivery_uuid=fallback_uuid,
            )
            return
        except Exception:
            self._log("post_fallback_failed", "FEISHU_POST_FALLBACK_FAILED")
        await self._transport.reply_content(
            self._source.message_id,
            json.dumps(
                {"text": render_text(answer, max_chars=self._max_content_chars)},
                ensure_ascii=False,
            ),
            message_type="text",
            delivery_uuid=fallback_uuid,
        )

    async def _remove_typing(self) -> None:
        reaction_id = self._reaction_id
        self._reaction_id = ""
        if not reaction_id:
            return
        try:
            await self._transport.remove_reaction(self._source.message_id, reaction_id)
        except Exception:
            self._log("typing_remove_failed", "FEISHU_TYPING_REMOVE_FAILED")

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _require_open(self, operation: str) -> None:
        if self._state is not ReplySessionState.OPEN:
            raise ReplySessionStateError(f"cannot {operation} a {self._state.value} reply")

    def _log(self, event: str, error_code: str) -> None:
        LOGGER.warning(
            "channel_event=%s channel=feishu account_id_hash=%s message_id_hash=%s card_visible=%s result=failed error_code=%s",
            event,
            _short_hash(self._source.account_id),
            _short_hash(self._source.message_id),
            str(self._card_visible).lower(),
            error_code,
        )

    def _log_latency(self, event: str, started_at: float) -> None:
        elapsed_ms = round((self._clock() - started_at) * 1000)
        LOGGER.info(
            "channel_event=%s channel=feishu account_id_hash=%s message_id_hash=%s elapsed_ms=%s result=ok error_code=",
            event,
            _short_hash(self._source.account_id),
            _short_hash(self._source.message_id),
            elapsed_ms,
        )


def _safe_markdown_link(match: re.Match[str]) -> str:
    label, url = match.group(1), match.group(2)
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or parsed.hostname is None or parsed.username or parsed.password:
        return label
    host = parsed.hostname
    return f"[{label} · {host}]({url})"


def _fence_tables(markdown: str) -> str:
    lines = markdown.splitlines()
    rendered: list[str] = []
    index = 0
    in_fence = False
    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            rendered.append(line)
            index += 1
            continue
        if not in_fence and index + 1 < len(lines) and "|" in line and _TABLE_SEPARATOR_RE.match(lines[index + 1]):
            table: list[str] = [line, lines[index + 1]]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                table.append(lines[index])
                index += 1
            rendered.extend(["```text", *table, "```"])
            continue
        rendered.append(line)
        index += 1
    return "\n".join(rendered)


def _has_unclosed_fence(markdown: str) -> bool:
    return sum(1 for line in markdown.splitlines() if line.lstrip().startswith("```")) % 2 == 1


def _truncate_utf8(content: str) -> str:
    encoded = content.encode("utf-8")
    if len(encoded) <= _CARD_BYTE_LIMIT:
        return content
    suffix = "\n\n（回答过长，卡片已截断）"
    budget = _CARD_BYTE_LIMIT - len(suffix.encode("utf-8"))
    prefix = encoded[:budget].decode("utf-8", errors="ignore").rstrip()
    return prefix + suffix


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
