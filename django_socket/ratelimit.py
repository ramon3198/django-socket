"""Incoming-message rate limiting, per socket.

    DJANGO_SOCKET = {"RATE_LIMIT": "60/m"}      # every route
    @ws("chat/", rate_limit="10/s")             # or just this one

It is a *token bucket*, not a per-window counter, and the difference is what
makes it usable: a counter rejects message 11 even when the previous 10 were 59
seconds ago. The bucket refills continuously, so it absorbs the normal burst of
someone typing fast and only cuts when the *sustained* rate goes over.

Running out closes the connection with **4429**. To let bigger spikes through,
raise the burst without raising the sustained rate:

    @ws("cursor/", rate_limit="30/s", burst=100)
"""

from __future__ import annotations

import re
import time

UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_FORMAT = re.compile(r"^\s*(\d+)\s*/\s*(\d*)\s*([smhd])\s*$", re.I)

CLOSE_RATE_LIMIT = 4429


def parse_rate(spec: str) -> tuple[float, float]:
    """
    ``"60/m"`` -> ``(60 messages, 60 seconds)``. Also ``"100/5m"``, ``"10/s"``.

    Returns ``(amount, period_in_seconds)``.
    """
    if not isinstance(spec, str):
        raise ValueError(f"rate_limit must be a string like '60/m', not {spec!r}")

    m = _FORMAT.match(spec)
    if not m:
        raise ValueError(
            f"Invalid rate_limit: {spec!r}. The format is '<amount>/<period>', "
            f"for example '60/m', '10/s' or '100/5m'."
        )

    amount, multiple, unit = m.groups()
    if int(amount) <= 0:
        raise ValueError(f"Invalid rate_limit: {spec!r}. The amount must be > 0.")
    return float(amount), float(multiple or 1) * UNITS[unit.lower()]


class TokenBucket:
    """A token bucket. `consume()` returns False once there is no room left."""

    __slots__ = ("capacity", "per_second", "remaining", "stamp")

    def __init__(self, amount: float, period: float, burst: float | None = None):
        self.capacity = float(burst if burst is not None else amount)
        self.per_second = amount / period
        self.remaining = self.capacity
        self.stamp = time.monotonic()

    def consume(self, cost: float = 1.0) -> bool:
        now = time.monotonic()
        self.remaining = min(
            self.capacity, self.remaining + (now - self.stamp) * self.per_second
        )
        self.stamp = now
        if self.remaining < cost:
            return False
        self.remaining -= cost
        return True

    @property
    def retry_after(self) -> float:
        """Seconds until there is room again. Useful to tell the client."""
        if self.remaining >= 1:
            return 0.0
        return (1 - self.remaining) / self.per_second


def make_bucket(spec=None, burst=None) -> TokenBucket | None:
    """A bucket for one route, or None if no limit is configured anywhere."""
    if spec is None:
        from django.conf import settings

        conf = getattr(settings, "DJANGO_SOCKET", {}) or {}
        spec = conf.get("RATE_LIMIT")
        burst = burst if burst is not None else conf.get("RATE_LIMIT_BURST")

    if not spec:
        return None
    amount, period = parse_rate(spec)
    return TokenBucket(amount, period, burst)
