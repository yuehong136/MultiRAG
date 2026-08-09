"""Feishu CardKit reply state, renderer and fallback contracts."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from typing import cast

import pytest
from beartype.roar import BeartypeCallHintParamViolation

from api.channels.core.base import IncomingMessage, ReplySessionState, ReplySessionStateError
from api.channels.core.reply import SERVICE_UNAVAILABLE_TEXT
from api.channels.feishu.reply import (
    FeishuProgressiveReplySession,
    FeishuReplyTransport,
    delivery_uuid,
    render_markdown,
    render_post,
    render_text,
    streaming_card_json,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Transport:
    def __init__(self, *, failures: set[str] | None = None) -> None:
        self.failures = failures or set()
        self.reactions_added: list[tuple[str, str]] = []
        self.reactions_removed: list[tuple[str, str]] = []
        self.cards: list[str] = []
        self.card_replies: list[tuple[str, str, str]] = []
        self.updates: list[tuple[str, str, int, str]] = []
        self.finishes: list[tuple[str, int, str]] = []
        self.fallback_attempts: list[tuple[str, str, str, str]] = []
        self.fallbacks: list[tuple[str, str, str, str]] = []

    async def add_typing_reaction(self, message_id: str) -> str:
        self._fail("reaction_add")
        self.reactions_added.append((message_id, "Typing"))
        return "reaction-1"

    async def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        self._fail("reaction_remove")
        self.reactions_removed.append((message_id, reaction_id))

    async def create_streaming_card(self, card_json: str) -> str:
        self._fail("card_create")
        self.cards.append(card_json)
        return "card-1"

    async def reply_card(
        self,
        message_id: str,
        card_id: str,
        *,
        delivery_uuid: str,
    ) -> str:
        self._fail("card_reply")
        self.card_replies.append((message_id, card_id, delivery_uuid))
        return "reply-1"

    async def update_card_text(
        self,
        card_id: str,
        content: str,
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> None:
        self._fail("card_update")
        self.updates.append((card_id, content, sequence, delivery_uuid))

    async def finish_streaming_card(
        self,
        card_id: str,
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> None:
        self._fail("card_finish")
        self.finishes.append((card_id, sequence, delivery_uuid))

    async def reply_content(
        self,
        message_id: str,
        content: str,
        *,
        message_type: str,
        delivery_uuid: str,
    ) -> None:
        self.fallback_attempts.append((message_id, content, message_type, delivery_uuid))
        self._fail(f"fallback_{message_type}")
        self.fallbacks.append((message_id, content, message_type, delivery_uuid))

    def _fail(self, operation: str) -> None:
        if operation in self.failures:
            raise RuntimeError(f"{operation} failed")


class _SlowReactionTransport(_Transport):
    def __init__(self) -> None:
        super().__init__()
        self.reaction_started = asyncio.Event()
        self.release_reaction = asyncio.Event()

    async def add_typing_reaction(self, message_id: str) -> str:
        self.reaction_started.set()
        await self.release_reaction.wait()
        self.reactions_added.append((message_id, "Typing"))
        return "reaction-1"


class _SlowUpdateTransport(_Transport):
    def __init__(self, clock: _Clock) -> None:
        super().__init__()
        self._clock = clock

    async def update_card_text(
        self,
        card_id: str,
        content: str,
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> None:
        await super().update_card_text(
            card_id,
            content,
            sequence=sequence,
            delivery_uuid=delivery_uuid,
        )
        self._clock.advance(2.0)


def _source() -> IncomingMessage:
    return IncomingMessage(
        channel="feishu",
        account_id="account-1",
        chat_id="chat-1",
        chat_type="p2p",
        message_id="message-1",
        sender_id="sender-1",
        content="question",
        sender_type="user",
        event_id="event-1",
    )


def test_reply_transport_is_runtime_checkable() -> None:
    assert isinstance(_Transport(), FeishuReplyTransport)
    assert not isinstance(object(), FeishuReplyTransport)


def test_reply_session_rejects_transport_that_misses_protocol() -> None:
    invalid_transport = cast(FeishuReplyTransport, object())

    with pytest.raises(BeartypeCallHintParamViolation):
        FeishuProgressiveReplySession(
            transport=invalid_transport,
            source=_source(),
            max_content_chars=4000,
        )


def test_reply_module_import_is_clean_under_beartype() -> None:
    script = """
import warnings
from beartype.roar import BeartypeClawDecorWarning
warnings.simplefilter("error", BeartypeClawDecorWarning)
import api.channels.feishu.reply
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


async def _session(
    transport: _Transport,
    clock: _Clock,
) -> FeishuProgressiveReplySession:
    return await FeishuProgressiveReplySession.begin(
        transport=transport,
        source=_source(),
        max_content_chars=4000,
        update_interval_seconds=0.25,
        clock=clock,
    )


def test_renderers_sanitize_links_mentions_tables_and_incomplete_fences() -> None:
    content = """# 标题
[@all](https://safe.example/path) @everyone <at id=\"all\">所有人</at>
[安全](https://example.com) [危险](http://example.com)
![外部图片](https://example.com/tracker.png)
| A | B |
|---|---|
| 1 | 2 |
```python
print('still streaming')"""

    markdown = render_markdown(content)
    text = render_text(content, max_chars=4000)
    post = json.loads(render_post(content, max_chars=4000))

    assert "https://example.com" in markdown
    assert "安全 · example.com" in markdown
    assert "http://example.com" not in markdown
    assert "tracker.png" not in markdown
    assert "＠all" in markdown
    assert "＠everyone" in markdown
    assert "<at" not in markdown
    assert "&lt;at" in markdown
    assert "<at" not in text
    assert "<at" not in json.dumps(post, ensure_ascii=False)
    assert "```text\n| A | B |" in markdown
    assert markdown.endswith("```")
    assert "标题" in text
    assert post["zh_cn"]["title"] == "MultiRAG"
    assert all(element[0]["tag"] == "text" for element in post["zh_cn"]["content"])


def test_streaming_card_is_json_2_with_one_stable_markdown_element() -> None:
    card = json.loads(streaming_card_json())

    assert card["schema"] == "2.0"
    assert card["config"]["streaming_mode"] is True
    assert card["body"]["elements"] == [
        {
            "tag": "markdown",
            "element_id": "answer",
            "content": "正在生成回答…",
        }
    ]


def test_delivery_uuid_is_deterministic_opaque_and_stage_scoped() -> None:
    source = _source()

    first = delivery_uuid(source, "running_card")
    assert first == delivery_uuid(source, "running_card")
    assert first != delivery_uuid(source, "final_fallback")
    assert len(first) == 40
    assert source.message_id not in first
    assert source.event_id not in first


@pytest.mark.asyncio
async def test_progressive_reply_throttles_updates_then_flushes_and_finishes_in_order() -> None:
    clock = _Clock()
    transport = _Transport()
    session = await _session(transport, clock)

    await session.append("第一段")
    clock.advance(0.249)
    await session.append("第二段")
    assert transport.updates == []

    clock.advance(0.001)
    await session.append("第三段")
    assert [update[2] for update in transport.updates] == [1]

    await session.append("第四段")
    assert [update[2] for update in transport.updates] == [1]
    await session.complete()

    assert session.state is ReplySessionState.COMPLETED
    assert [update[2] for update in transport.updates] == [1, 2]
    assert transport.updates[-1][1] == "第一段第二段第三段第四段"
    assert [finish[1] for finish in transport.finishes] == [3]
    assert transport.fallbacks == []
    assert transport.reactions_added == [("message-1", "Typing")]
    assert transport.reactions_removed == [("message-1", "reaction-1")]
    operation_uuids = [transport.card_replies[0][2], *(item[3] for item in transport.updates), transport.finishes[0][2]]
    assert len(operation_uuids) == len(set(operation_uuids))


@pytest.mark.asyncio
async def test_short_model_deltas_are_coalesced_after_the_first_visible_text() -> None:
    clock = _Clock()
    transport = _Transport()
    session = await _session(transport, clock)
    clock.advance(0.25)

    await session.append("首屏")
    assert [update[1] for update in transport.updates] == ["首屏"]

    for delta in ("甲乙丙丁", "戊己庚辛", "壬癸子丑"):
        clock.advance(0.25)
        await session.append(delta)
    assert [update[1] for update in transport.updates] == ["首屏"]

    clock.advance(0.25)
    await session.append("寅卯辰巳")

    assert [update[1] for update in transport.updates] == [
        "首屏",
        "首屏甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳",
    ]


@pytest.mark.asyncio
async def test_small_pending_delta_flushes_at_the_maximum_wait() -> None:
    clock = _Clock()
    transport = _Transport()
    session = await _session(transport, clock)
    clock.advance(0.25)
    await session.append("首屏")

    clock.advance(1.0)
    await session.append("少量")

    assert [update[1] for update in transport.updates] == ["首屏", "首屏少量"]


@pytest.mark.asyncio
async def test_cardkit_round_trip_does_not_trigger_a_back_to_back_tiny_patch() -> None:
    clock = _Clock()
    transport = _SlowUpdateTransport(clock)
    session = await _session(transport, clock)
    clock.advance(0.25)
    await session.append("首屏")
    assert clock.now == 102.25

    await session.append("甲乙丙丁")
    assert [update[1] for update in transport.updates] == ["首屏"]

    clock.advance(1.0)
    await session.append("戊己庚辛")
    assert [update[1] for update in transport.updates] == ["首屏", "首屏甲乙丙丁戊己庚辛"]


@pytest.mark.asyncio
async def test_reaction_failure_is_best_effort_and_does_not_disable_card() -> None:
    transport = _Transport(failures={"reaction_add"})
    session = await _session(transport, _Clock())

    await session.append("回答")
    await session.complete()

    assert transport.card_replies
    assert transport.updates[-1][1] == "回答"
    assert transport.finishes
    assert transport.fallbacks == []


@pytest.mark.asyncio
async def test_slow_reaction_never_blocks_first_card_and_is_cleaned_up_after_terminal() -> None:
    transport = _SlowReactionTransport()

    session = await _session(transport, _Clock())

    assert transport.reaction_started.is_set()
    assert transport.card_replies
    assert transport.reactions_added == []
    await session.append("回答")
    await session.complete()
    assert session.state is ReplySessionState.COMPLETED

    transport.release_reaction.set()
    await asyncio.sleep(0)
    assert transport.reactions_added == [("message-1", "Typing")]
    assert transport.reactions_removed == [("message-1", "reaction-1")]


@pytest.mark.asyncio
async def test_card_creation_failure_falls_back_to_one_post_without_losing_answer() -> None:
    transport = _Transport(failures={"card_create"})
    session = await _session(transport, _Clock())

    await session.append("完整回答")
    await session.complete()

    assert len(transport.fallbacks) == 1
    message_id, content, message_type, fallback_uuid = transport.fallbacks[0]
    assert (message_id, message_type) == ("message-1", "post")
    assert "完整回答" in content
    assert fallback_uuid == delivery_uuid(_source(), "final_fallback")
    assert transport.card_replies == []


@pytest.mark.asyncio
async def test_final_card_patch_failure_falls_back_post_then_text_with_same_uuid() -> None:
    transport = _Transport(failures={"card_update", "fallback_post"})
    session = await _session(transport, _Clock())

    await session.append("最终回答")
    await session.complete()

    assert len(transport.fallbacks) == 1
    _, content, message_type, fallback_uuid = transport.fallbacks[0]
    assert message_type == "text"
    assert json.loads(content) == {"text": "最终回答"}
    assert fallback_uuid == delivery_uuid(_source(), "final_fallback")
    assert [attempt[2] for attempt in transport.fallback_attempts] == ["post", "text"]
    assert len({attempt[3] for attempt in transport.fallback_attempts}) == 1
    assert transport.finishes == []


@pytest.mark.asyncio
async def test_intermediate_card_patch_failure_keeps_buffering_the_complete_answer() -> None:
    clock = _Clock()
    transport = _Transport(failures={"card_update"})
    session = await _session(transport, clock)
    await session.append("第一段")
    clock.advance(0.25)

    await session.append("第二段")
    await session.append("第三段")
    await session.complete()

    assert session.state is ReplySessionState.COMPLETED
    assert len(transport.fallbacks) == 1
    assert "第一段第二段第三段" in transport.fallbacks[0][1]
    assert transport.finishes == []


@pytest.mark.asyncio
async def test_finish_failure_does_not_duplicate_already_visible_final_answer() -> None:
    transport = _Transport(failures={"card_finish"})
    session = await _session(transport, _Clock())

    await session.append("最终回答")
    await session.complete()

    assert transport.updates[-1][1] == "最终回答"
    assert transport.fallbacks == []
    assert transport.reactions_removed == [("message-1", "reaction-1")]


@pytest.mark.asyncio
async def test_execution_failure_replaces_partial_card_with_safe_terminal_text() -> None:
    clock = _Clock()
    transport = _Transport()
    session = await _session(transport, clock)
    clock.advance(0.25)
    await session.append("不能作为最终结果的半截内容")

    await session.fail("TARGET_EXECUTION_FAILED")

    assert session.state is ReplySessionState.FAILED
    assert transport.updates[-1][1] == SERVICE_UNAVAILABLE_TEXT
    assert [update[2] for update in transport.updates] == [1, 2]
    assert transport.finishes[0][1] == 3
    assert transport.fallbacks == []
    with pytest.raises(ReplySessionStateError):
        await session.append("late")


@pytest.mark.asyncio
async def test_card_and_both_fallback_failures_propagate_in_terminal_state() -> None:
    transport = _Transport(
        failures={"card_create", "fallback_post", "fallback_text"},
    )
    session = await _session(transport, _Clock())
    await session.append("回答")

    with pytest.raises(RuntimeError, match="fallback_text failed"):
        await session.complete()

    assert session.state is ReplySessionState.COMPLETED
    assert transport.reactions_removed == [("message-1", "reaction-1")]
    with pytest.raises(ReplySessionStateError):
        await session.complete()
