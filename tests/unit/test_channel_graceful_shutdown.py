"""Cooperative shutdown across worker, private SSE, bridge and reply session.

Every test here drives the real chain -- ``ChannelWorker`` queue ->
``BindingBridge`` -> ``MultiRAGBindingExecutionClient`` over the private
HTTP/SSE boundary -> ``FeishuProgressiveReplySession`` -- because the fact
CHN-U16 has to establish is about all four at once: what the user's card ends
up saying, and whether the target was ever asked to run. Asserting that some
internal task was cancelled would prove neither.

Scope is the cooperative window only (normal SIGTERM, supervisor generation
switch, explicit close). A killed process has no such window and is not
claimed here; that stays with the suspended CHN-O14.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from api.channel_capabilities import EffectiveReplyCapabilities
from api.channels.binding_bridge import QUEUE_BUSY_TEXT, BindingBridge
from api.channels.core.base import (
    Channel,
    ChannelAction,
    IncomingMessage,
    OutgoingMessage,
    ReplyContext,
    ReplySessionState,
)
from api.channels.feishu.reply import CARD_ACTIONS_ELEMENT_ID, CARD_STATUS_ELEMENT_ID, FeishuProgressiveReplySession
from api.channels.reply_session import CANCELLED_TEXT
from api.channels.runtime_client import MultiRAGBindingExecutionClient
from api.channels.worker import ChannelWorker

_FULL_CAPABILITIES = EffectiveReplyCapabilities(
    progressive_reply=True,
    cancel_queued=True,
    cancel_running=True,
    regenerate=True,
    retry=True,
    feedback=True,
)
_CANCELLED_STATUS_TEXT = "⏹️ 已停止生成 · 外部操作不视为已撤销"
_EXECUTION_URL = "http://multirag.local/api/v1/internal/channel-bindings/binding-1/executions"


class _Card:
    """One provider card, recorded exactly as the transport would see it."""

    def __init__(self, card_id: str) -> None:
        self.card_id = card_id
        self.texts: list[str] = []
        self.status_texts: list[str] = []
        self.action_ids: dict[str, str] = {}
        self.finish_calls = 0

    @property
    def finished(self) -> bool:
        return self.finish_calls > 0

    @property
    def visible_answer(self) -> str:
        return self.texts[-1] if self.texts else ""

    @property
    def visible_status(self) -> str:
        return self.status_texts[-1] if self.status_texts else ""


class _FeishuTransport:
    """Minimal CardKit transport that keeps every card's visible history."""

    def __init__(self) -> None:
        self.cards: list[_Card] = []
        self.fallbacks: list[str] = []
        self.reactions_added = 0
        self.reactions_removed = 0

    async def add_typing_reaction(self, message_id: str) -> str:
        del message_id
        self.reactions_added += 1
        return f"reaction-{self.reactions_added}"

    async def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        del message_id, reaction_id
        self.reactions_removed += 1

    async def create_streaming_card(self, card_json: str) -> str:
        card = _Card(f"card-{len(self.cards) + 1}")
        for element in json.loads(card_json)["body"]["elements"]:
            if element.get("element_id") == CARD_STATUS_ELEMENT_ID:
                card.status_texts.append(str(element["content"]))
            elif element.get("element_id") == CARD_ACTIONS_ELEMENT_ID:
                card.action_ids = _rendered_action_ids(element)
        self.cards.append(card)
        return card.card_id

    async def reply_card(self, message_id: str, card_id: str, *, delivery_uuid: str) -> str:
        del message_id, delivery_uuid
        return f"reply-{card_id}"

    async def update_card_text(self, card_id: str, content: str, *, sequence: int, delivery_uuid: str) -> None:
        del sequence, delivery_uuid
        self._card(card_id).texts.append(content)

    async def batch_update_card(
        self,
        card_id: str,
        actions: list[dict[str, object]],
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> None:
        del sequence, delivery_uuid
        card = self._card(card_id)
        for action in actions:
            params = action.get("params")
            if not isinstance(params, dict) or params.get("element_id") != CARD_STATUS_ELEMENT_ID:
                continue
            element = params.get("element")
            if isinstance(element, dict):
                card.status_texts.append(str(element["content"]))

    async def finish_streaming_card(self, card_id: str, *, sequence: int, delivery_uuid: str) -> None:
        del sequence, delivery_uuid
        self._card(card_id).finish_calls += 1

    async def reply_content(self, message_id: str, content: str, *, message_type: str, delivery_uuid: str) -> None:
        del message_id, message_type, delivery_uuid
        self.fallbacks.append(content)

    def _card(self, card_id: str) -> _Card:
        for card in self.cards:
            if card.card_id == card_id:
                return card
        raise AssertionError(f"unknown card {card_id}")


def _rendered_action_ids(actions_element: dict[str, object]) -> dict[str, str]:
    """Read the opaque action IDs straight off the rendered card wire."""

    rendered: dict[str, str] = {}
    for column in actions_element.get("columns", []) or []:
        for button in column.get("elements", []):
            element_id = str(button["element_id"]).removeprefix("reply_")
            rendered[element_id] = str(button["behaviors"][0]["value"]["action_id"])
    return rendered


class _ReplyChannel(Channel):
    """A provider whose replies are real progressive Feishu card sessions."""

    channel_id = "feishu"
    account_id = "account-1"

    def __init__(self) -> None:
        super().__init__()
        self.transport = _FeishuTransport()
        self.sent: list[OutgoingMessage] = []
        self.sessions: list[FeishuProgressiveReplySession] = []
        self.is_running = False

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        self.is_running = False

    async def send(self, message: OutgoingMessage) -> None:
        self.sent.append(message)

    async def begin_reply(
        self,
        source: IncomingMessage,
        *,
        max_content_chars: int,
        context: ReplyContext | None = None,
    ) -> FeishuProgressiveReplySession:
        session = await FeishuProgressiveReplySession.begin(
            transport=self.transport,
            source=source,
            max_content_chars=max_content_chars,
            context=context,
            update_interval_seconds=0.01,
        )
        self.sessions.append(session)
        return session


class _StateStore:
    def __init__(self) -> None:
        self.claimed: set[str] = set()
        self.status: dict[str, str] = {}

    async def claim_message(self, message_id: str) -> bool:
        if message_id in self.claimed:
            return False
        self.claimed.add(message_id)
        self.status[message_id] = "processing"
        return True

    async def mark_replied(self, message_id: str) -> None:
        self.status[message_id] = "replied"

    async def mark_executed(self, message_id: str) -> None:
        self.status[message_id] = "executed"

    async def mark_failed(self, message_id: str) -> None:
        self.status[message_id] = "failed"

    async def get_session(self, conversation: str) -> str | None:
        del conversation
        return None

    async def put_session(self, conversation: str, session_id: str, *, ttl_seconds: int | None = None) -> None:
        del conversation, session_id, ttl_seconds

    async def reset_session(self, conversation: str) -> None:
        del conversation


class _LeaderStore:
    leader_renew_interval_seconds = 3600

    def __init__(self) -> None:
        self.released: list[str] = []

    async def acquire_leader(self, *, lease_name: str) -> str | None:
        del lease_name
        return "owner-token"

    async def renew_leader(self, owner_token: str, *, lease_name: str) -> bool:
        del owner_token, lease_name
        return True

    async def release_leader(self, owner_token: str, *, lease_name: str) -> bool:
        del lease_name
        self.released.append(owner_token)
        return True


class _Redis:
    def __init__(self) -> None:
        self.closed = False

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed = True


class _GatedSSEStream(httpx.AsyncByteStream):
    """An execution stream that stalls until the test lets it finish."""

    def __init__(self, *, tail: bytes) -> None:
        self.first_delta = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self._tail = tail

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'data:{"event":"message_delta","content":"partial","session_id":"session-1"}\n\n'
        self.first_delta.set()
        await self.release.wait()
        yield self._tail

    async def aclose(self) -> None:
        self.closed = True


class _ExecutionAPI:
    """The private MultiRAG API side of the boundary, recording every call."""

    def __init__(self, *, tail: bytes | None = None) -> None:
        self.streams: list[_GatedSSEStream] = []
        self.executed_event_ids: list[str] = []
        self._tail = tail if tail is not None else b'data:{"event":"message_completed","session_id":"session-1"}\n\ndata:[DONE]\n\n'

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/system/ping"):
            return httpx.Response(200, text='"pong"')
        assert str(request.url) == _EXECUTION_URL
        self.executed_event_ids.append(json.loads(request.content)["event_id"])
        stream = _GatedSSEStream(tail=self._tail)
        self.streams.append(stream)
        return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})


def _message(message_id: str) -> IncomingMessage:
    return IncomingMessage(
        channel="feishu",
        account_id="account-1",
        chat_id="oc-chat",
        chat_type="p2p",
        message_id=message_id,
        sender_id="ou-user",
        content="hello",
        message_type="text",
        sender_type="user",
    )


class _Harness:
    def __init__(
        self,
        *,
        api: _ExecutionAPI,
        followup_queue_size: int = 5,
        queue_size: int = 10,
        capabilities: EffectiveReplyCapabilities = _FULL_CAPABILITIES,
    ) -> None:
        self.api = api
        self.channel = _ReplyChannel()
        self.state = _StateStore()
        self.leader = _LeaderStore()
        self.redis = _Redis()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(api))
        self.executor = MultiRAGBindingExecutionClient(
            base_url="http://multirag.local",
            binding_id="binding-1",
            binding_generation=3,
            api_token="runtime-token",
            client=self.http,
        )
        self.bridge = BindingBridge(
            channel=self.channel,
            executor=self.executor,
            state_store=self.state,
            binding_id="binding-1",
            allowed_sender_ids=set(),
            max_question_chars=100,
            max_answer_chars=4000,
            capabilities=capabilities,
        )
        self.worker = ChannelWorker(
            provider_name="feishu",
            channel=self.channel,
            bridge=self.bridge,
            agent_client=self.executor,
            state_store=self.leader,
            redis=self.redis,
            queue_size=queue_size,
            worker_concurrency=1,
            followup_queue_size=followup_queue_size,
        )
        self.stop_event = asyncio.Event()
        self.run_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.run_task = asyncio.create_task(self.worker.run(self.stop_event))
        await _until(lambda: self.channel.is_running)

    async def deliver(self, message: IncomingMessage) -> None:
        handler = self.channel._handler
        assert handler is not None
        await handler(message)

    async def stop(self) -> None:
        self.stop_event.set()
        assert self.run_task is not None
        # Generous on purpose: a shutdown that is merely slow must still be
        # judged on the card it leaves behind, not on this timeout.
        await asyncio.wait_for(self.run_task, timeout=20)

    async def aclose(self) -> None:
        await self.http.aclose()

    @property
    def cards(self) -> list[_Card]:
        return self.channel.transport.cards


async def _until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was never reached")
        await asyncio.sleep(0.005)


def _live_channel_tasks() -> list[str]:
    return [task.get_name() for task in asyncio.all_tasks() if task.get_name().startswith("channel-") and not task.done()]


@pytest.mark.asyncio
async def test_normal_stop_gives_a_queued_reply_a_terminal_card_and_never_starts_it() -> None:
    api = _ExecutionAPI()
    harness = _Harness(api=api)
    await harness.start()

    await harness.deliver(_message("message-running"))
    await _until(lambda: bool(api.streams))
    await asyncio.wait_for(api.streams[0].first_delta.wait(), timeout=2)
    await harness.deliver(_message("message-queued"))
    await _until(lambda: len(harness.cards) == 2)

    await harness.stop()
    await harness.aclose()

    queued_card = harness.cards[1]
    # The only fact that matters to the user: the queued card is finished and
    # says it stopped, rather than sitting on "queued" until someone notices.
    assert queued_card.finished is True
    assert queued_card.visible_status == _CANCELLED_STATUS_TEXT
    assert queued_card.visible_answer == CANCELLED_TEXT
    assert queued_card.finish_calls == 1
    assert queued_card.status_texts.count(_CANCELLED_STATUS_TEXT) == 1
    # And it reached that state without the target ever being asked to run.
    assert api.executed_event_ids == ["message-running"]
    assert harness.state.status == {
        "message-running": "replied",
        "message-queued": "replied",
    }
    assert harness.channel.sessions[1].state is ReplySessionState.CANCELLED


@pytest.mark.asyncio
async def test_normal_stop_closes_the_running_execution_stream_and_finishes_its_card() -> None:
    api = _ExecutionAPI()
    harness = _Harness(api=api)
    await harness.start()

    await harness.deliver(_message("message-running"))
    await _until(lambda: bool(api.streams))
    await asyncio.wait_for(api.streams[0].first_delta.wait(), timeout=2)

    await harness.stop()
    await harness.aclose()

    # The private SSE response is released by the shutdown itself, not left to
    # whenever the loop finalizes an abandoned generator.
    assert api.streams[0].closed is True
    assert api.streams[0].release.is_set() is False
    running_card = harness.cards[0]
    assert running_card.finished is True
    assert running_card.visible_status == _CANCELLED_STATUS_TEXT
    assert running_card.visible_answer == CANCELLED_TEXT
    assert harness.channel.sessions[0].state is ReplySessionState.CANCELLED
    assert harness.channel.transport.fallbacks == []
    assert harness.state.status == {"message-running": "replied"}
    assert harness.leader.released == ["owner-token"]
    assert harness.redis.closed is True
    assert _live_channel_tasks() == []


@pytest.mark.asyncio
async def test_stop_lets_a_committed_answer_finish_delivering() -> None:
    api = _ExecutionAPI(
        tail=b'data:{"event":"message_completed","content":"committed answer","session_id":"session-1"}\n\ndata:[DONE]\n\n',
    )
    harness = _Harness(api=api)
    await harness.start()

    await harness.deliver(_message("message-running"))
    await _until(lambda: bool(api.streams))
    await asyncio.wait_for(api.streams[0].first_delta.wait(), timeout=2)
    api.streams[0].release.set()
    # The target committed and the card is mid-delivery when the stop lands.
    await _until(lambda: harness.channel.transport.cards[0].finished)

    await harness.stop()
    await harness.aclose()

    # A committed target must not be reported to the user as stopped.
    card = harness.cards[0]
    assert card.visible_answer == "committed answer"
    assert card.visible_status == "✅ 回答完成"
    assert harness.channel.sessions[0].state is ReplySessionState.COMPLETED
    assert harness.state.status == {"message-running": "replied"}


@pytest.mark.asyncio
async def test_close_is_idempotent_and_leaves_no_bridge_owned_tasks() -> None:
    api = _ExecutionAPI()
    harness = _Harness(api=api)
    await harness.start()

    await harness.deliver(_message("message-running"))
    await _until(lambda: bool(api.streams))
    await asyncio.wait_for(api.streams[0].first_delta.wait(), timeout=2)
    await harness.deliver(_message("message-queued"))
    await _until(lambda: len(harness.cards) == 2)

    await harness.stop()
    settled = [(card.finish_calls, card.visible_status, card.visible_answer) for card in harness.cards]

    await harness.worker.close()
    await harness.bridge.close()
    await harness.aclose()

    # Both cards are terminal after the first stop, and the two extra closes
    # change nothing a user could see: no second finish, no second card write.
    assert settled == [(1, _CANCELLED_STATUS_TEXT, CANCELLED_TEXT)] * 2
    assert [(card.finish_calls, card.visible_status, card.visible_answer) for card in harness.cards] == settled
    assert api.executed_event_ids == ["message-running"]
    assert _live_channel_tasks() == []


@pytest.mark.asyncio
async def test_stopping_a_queued_card_by_hand_also_never_starts_execution() -> None:
    api = _ExecutionAPI()
    harness = _Harness(api=api)
    await harness.start()

    await harness.deliver(_message("message-running"))
    await _until(lambda: bool(api.streams))
    await asyncio.wait_for(api.streams[0].first_delta.wait(), timeout=2)
    await harness.deliver(_message("message-queued"))
    await _until(lambda: len(harness.cards) == 2)

    response = await harness.bridge.handle_action(
        ChannelAction(
            action_id=harness.cards[1].action_ids["cancel"],
            operator_id="ou-user",
            chat_id="oc-chat",
            message_id="reply-card-2",
            event_id="cancel-event-1",
        )
    )
    await _until(lambda: harness.cards[1].finished)

    assert response.toast_type == "success"
    assert harness.cards[1].visible_status == _CANCELLED_STATUS_TEXT
    assert api.executed_event_ids == ["message-running"]

    api.streams[0].release.set()
    await harness.stop()
    await harness.aclose()

    # Releasing the queue after a cancel must not resurrect the cancelled turn.
    assert api.executed_event_ids == ["message-running"]
    assert harness.cards[1].finish_calls == 1


@pytest.mark.asyncio
async def test_queue_overflow_tells_the_user_instead_of_dropping_silently() -> None:
    api = _ExecutionAPI()
    harness = _Harness(api=api, followup_queue_size=1)
    await harness.start()

    await harness.deliver(_message("message-running"))
    await _until(lambda: bool(api.streams))
    await asyncio.wait_for(api.streams[0].first_delta.wait(), timeout=2)
    await harness.deliver(_message("message-followup"))
    await harness.deliver(_message("message-overflow"))
    await _until(lambda: bool(harness.channel.sent))

    assert harness.channel.sent == [
        OutgoingMessage(
            chat_id="oc-chat",
            content=QUEUE_BUSY_TEXT,
            reply_to_message_id="message-overflow",
        )
    ]
    assert harness.state.status["message-overflow"] == "replied"
    # The overflowing turn is answered, never executed, and gets no card.
    assert len(harness.cards) == 2
    assert api.executed_event_ids == ["message-running"]

    await harness.stop()
    await harness.aclose()
