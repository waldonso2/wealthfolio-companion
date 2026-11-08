"""One rule for "too many requests" (HTTP 429/503, waldonso2/wealthfolio-importer-addon#149).

``retry_after`` reads how long a server asks us to wait (seconds or an HTTP date, capped at ``MAX_WAIT``).
``with_backoff`` repeats an idempotent read after such a wait, at most ``len(waits)`` times, and then returns
the answer as it is - the caller raises its error then (a broker adapter: ``base.RateLimited``, which stops
that broker's run and makes the next scheduled run wait, ``Syncer._run_broker``).

Never use it for a login, a token, password or refresh grant, a TAN or challenge request, the activation of a
push TAN or ``complete_login``: a second try there counts against the bank's lock-out counters.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

log = logging.getLogger(__name__)

STATUSES = (429, 503)
WAITS: tuple[float, ...] = (2, 5, 15)
MAX_WAIT = 60.0
# After the last try: how long the next scheduled run waits without a Retry-After, and at most.
NEXT_TRY = 15 * 60.0
MAX_NEXT = 24 * 3600.0
# Replaced in tests, so they don't wait.
sleep: Callable[[float], None] = time.sleep


def limited(response: httpx.Response) -> bool:
    return response.status_code in STATUSES


def retry_after(response: httpx.Response, default: float, cap: float = MAX_WAIT,
                now: datetime | None = None) -> float:
    """Seconds from ``Retry-After`` (seconds or HTTP date), else ``default``; between 0 and ``cap``."""
    value = (response.headers.get("retry-after") or "").strip()
    seconds = default
    if value.isdigit():
        seconds = float(value)
    elif value:
        try:
            moment = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            moment = None
        if moment is not None:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            seconds = (moment - (now or datetime.now(UTC))).total_seconds()
    return min(max(seconds, 0.0), cap)


def with_backoff(send: Callable[[], httpx.Response], *, waits: Sequence[float] = WAITS,
                 cap: float = MAX_WAIT, statuses: Sequence[int] = STATUSES) -> httpx.Response:
    """``send()``; on 429/503 (or ``statuses``) wait (Retry-After, else the next of ``waits``, plus up to 20 %
    jitter) and send again; after the last wait the answer as it is."""
    r = send()
    for attempt, wait in enumerate(waits, 1):
        if r.status_code not in statuses:
            return r
        pause = min(retry_after(r, wait, cap) * (1 + random.random() * 0.2), cap)  # noqa: S311 - jitter, no secret
        # The path only: query parameters can carry tokens.
        log.info("rate limited (HTTP %d) at %s, waiting %.1fs (attempt %d)", r.status_code, r.request.url.path,
                 pause, attempt)
        sleep(pause)
        r = send()
    return r
