"""Agent streaming responses own their iterators until cleanup completes."""

from collections.abc import AsyncIterable, Awaitable, Callable
from typing import Any

from anyio import CancelScope
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send


class AgentStreamingResponse(StreamingResponse):
    def __init__(self, content: AsyncIterable[Any], *, close_callback: Callable[[], Awaitable[None]] | None = None, **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self.close_callback = close_callback

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # A disconnect during send leaves an async generator suspended at
            # yield. Starlette does not close it; do so before DB dependencies
            # exit, including failures before the first body frame is sent.
            with CancelScope(shield=True):
                try:
                    close = getattr(self.body_iterator, "aclose", None)
                    if close is not None:
                        await close()
                finally:
                    if self.close_callback is not None:
                        await self.close_callback()
