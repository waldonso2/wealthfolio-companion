"""Whether a newer release of the service exists - asked at most once a day, after a run.

Only the redirect of GitHub's ``releases/latest`` page is read (no API, no token, nothing about
the user is sent). The answer is kept in ``meta`` (``UPDATE``); the overview shows a newer
version, and a newly found one is notified once. ``COMPANION_UPDATE_CHECK=0`` turns it off.
Installing is the user's ``update`` in the container's console, or - switched on on the page *Updates* - the
container's own updater (``deploy/companion-autoupdate``, root, every minute), which asks ``due``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta

import httpx

from . import meta_keys
from .model import when
from .state import State

log = logging.getLogger(__name__)

REPO = "https://github.com/waldonso2/wealthfolio-companion"
RELEASES = f"{REPO}/releases"
EVERY = timedelta(days=1)
MIN_INTERVAL, MAX_INTERVAL = 60, 86400  # seconds between two questions of the updater
KEY = meta_keys.UPDATE()


VERSION = r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?"  # PEP 440 releases and pre-releases (0.1.0a1, 0.1.0b2, 0.1.0rc1)
PRE = {"a": 0, "b": 1, "rc": 2}  # a release itself sorts after all of its pre-releases


def _parse(version: str) -> tuple[int, ...] | None:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?", version.strip())
    if not m:
        return None
    pre = (PRE[m.group(4)], int(m.group(5))) if m.group(4) else (len(PRE), 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), *pre)


def newer(latest: str, current: str) -> bool:
    a, b = _parse(latest), _parse(current)
    return bool(a and b and a > b)


HOPS = 3  # a renamed repository answers with one more redirect to its new name first


def latest(http: httpx.Client) -> str | None:
    """The newest release's version (``0.27.0``), None when GitHub doesn't say. Redirects are followed only
    within github.com (the repository may be renamed), up to the first ``/releases/tag/v…``."""
    url = f"{RELEASES}/latest"
    for _ in range(HOPS):
        location = http.get(url, follow_redirects=False).headers.get("location", "")
        m = re.search(rf"/releases/tag/v({VERSION})$", location)
        if m:
            return m.group(1)
        target = httpx.URL(url).join(location) if location else None
        if target is None or target.scheme != "https" or target.host != "github.com":
            return None
        url = str(target)
    return None


API = "https://api.github.com/repos/waldonso2/wealthfolio-companion/releases/tags/v{version}"
MAX_DELAY = 30  # days an automatic update may wait after a release is published
ASK_EVERY = 900  # with a delay the release's date is asked each time (GitHub's API allows 60 requests an hour)


def age_days(http: httpx.Client, version: str) -> float | None:
    """Days since GitHub published ``version``, None when it doesn't say. A renamed repository answers with a
    redirect first: followed only within api.github.com."""
    url = API.format(version=version)
    for _ in range(HOPS):
        r = http.get(url, headers={"Accept": "application/vnd.github+json"}, follow_redirects=False)
        if r.is_redirect:
            target = httpx.URL(url).join(r.headers.get("location", ""))
            if target.scheme != "https" or target.host != "api.github.com":
                return None
            url = str(target)
            continue
        if r.status_code != 200:
            return None
        try:
            published = when(r.json()["published_at"])
        except (ValueError, KeyError, TypeError):
            return None
        return (datetime.now(UTC) - published).total_seconds() / 86400
    return None


PRODUCT = "wealthfolio-companion"  # an answer stored by Wealthfolio Broker Sync (restored with its backup) has none


def stored(state: State) -> dict:
    """The last answer, if it was found for this product: Broker Sync's 0.4x versions would count as newer."""
    stored = json.loads(state.meta(KEY) or "{}")
    return stored if stored.get("product") == PRODUCT else {}


def available(state: State, current: str) -> str | None:
    """The newer version found by the last check, if any."""
    found = stored(state).get("version")
    return found if found and newer(found, current) else None


def check(state: State, current: str, *, http: httpx.Client | None = None) -> str | None:
    """Asks GitHub when the last answer is older than a day; returns a newer version found for the
    first time (to notify once), else None. Never raises: no answer only means no hint."""
    if os.environ.get("COMPANION_UPDATE_CHECK", "1") == "0":
        return None
    last = stored(state)
    if last.get("checked") and datetime.now(UTC) - when(last["checked"]) < EVERY:
        return None
    client = http or httpx.Client(timeout=10)
    try:
        version = latest(client)
    except httpx.HTTPError as e:
        log.info("update check: GitHub not reachable (%s)", type(e).__name__)
        version = None
    finally:
        if http is None:
            client.close()
    state.set_meta(KEY, json.dumps({"version": version or last.get("version"), "product": PRODUCT,
                                    "checked": datetime.now(UTC).isoformat()}))
    if version and newer(version, current) and version != last.get("version"):
        return version
    return None


def due(auto_update: bool, interval: int, current: str, since: float, *, delay: int = 0,
        http: httpx.Client | None = None) -> tuple[bool, str | None]:
    """For the container's updater: whether GitHub is asked now (``since``: seconds since it last was) and the
    newer release to install. With ``delay`` (days) a release is installed only once it has been published that
    long - a faulty or tampered release meets the people who installed at once first; a date GitHub doesn't give
    means no update yet. Never raises: no answer means nothing to install."""
    if not auto_update or since < max(interval, MIN_INTERVAL, ASK_EVERY if delay else 0):
        return False, None
    client = http or httpx.Client(timeout=10)
    try:
        version = latest(client)
        if version and newer(version, current) and delay:
            age = age_days(client, version)
            if age is None or age < delay:
                log.info("update check: %s is not %d days old yet", version, delay)
                version = None
    except httpx.HTTPError as e:
        log.info("update check: GitHub not reachable (%s)", type(e).__name__)
        version = None
    finally:
        if http is None:
            client.close()
    return True, version if version and newer(version, current) else None
