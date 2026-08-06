"""Cooldown for the channel credential self-check (CHN-O6).

The self-check turns one authenticated request into one outbound call to a
third party, made with a tenant's own stored credential. Without a cooldown an
admin holding down a button is an unbounded relay -- and the party that gets
throttled first is that tenant's own provider app.
"""

from __future__ import annotations

import time
from typing import Final

_COOLDOWN_SECONDS: Final = 10.0
# Only caps a pathological spread across many channels inside one window;
# ordinary usage never approaches it, and expired rows are dropped on the way.
_MAX_TRACKED_CHANNELS: Final = 4096


class VerificationThrottle:
    """Admits one self-check per channel per cooldown window.

    **Per API process** is the honest caveat: N workers admit up to N calls per
    window. Deliberate for now -- the control plane's only stateful dependency
    is Postgres, and taking on Redis just to share a cooldown is a coupling
    that deserves its own decision rather than a side effect of this endpoint.
    The bound that matters is the one that exists: unbounded became bounded.
    """

    def __init__(self, *, cooldown_seconds: float = _COOLDOWN_SECONDS) -> None:
        self._cooldown_seconds = cooldown_seconds
        self._last_attempt: dict[tuple[str, str], float] = {}

    def admit(self, *, tenant_id: str, channel_id: str, now: float | None = None) -> bool:
        """Record and allow an attempt, or refuse one made too soon."""

        moment = time.monotonic() if now is None else now
        self._forget_expired(moment)
        key = (tenant_id, channel_id)
        previous = self._last_attempt.get(key)
        if previous is not None and moment - previous < self._cooldown_seconds:
            return False
        self._last_attempt[key] = moment
        return True

    def _forget_expired(self, moment: float) -> None:
        if len(self._last_attempt) < _MAX_TRACKED_CHANNELS:
            return
        self._last_attempt = {key: attempted_at for key, attempted_at in self._last_attempt.items() if moment - attempted_at < self._cooldown_seconds}


# One per process. The service is built per request, so the cooldown cannot
# live on it; tests inject their own instance instead of reaching in here.
VERIFICATION_THROTTLE: Final = VerificationThrottle()
