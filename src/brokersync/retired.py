"""Removes what an installation still holds of brokers this version no longer has.

The test broker "Dummy" (key ``dummy``) existed up to 0.3.6; N26 over its app's interface (key ``n26``)
up to 0.1.0b15 - N26 switched that interface off (``/api/smrt/transactions`` in 2023, ``/api/accounts`` in 2024),
the way to N26 is "Bank über PSD2". Their config, saved
credentials and session, sync state, runs, unknown events and holdings check
are removed on the next start. What it booked in Wealthfolio stays - deleting
activities is the user's call; the overview shows once how many there are and
how to find them (comment ``[SYNC <key>:``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from . import config as config_mod
from . import meta_keys
from .state import State
from .vault import Vault

log = logging.getLogger(__name__)

RETIRED = {"dummy": "Dummy (Test)", "n26": "N26 (App-Schnittstelle)"}


def clean_up(data_dir: Path, state: State, vault: Vault) -> None:
    if any(k in config_mod.load(data_dir).brokers for k in RETIRED):
        def drop_config(cfg: config_mod.Config) -> None:
            for key in RETIRED:
                cfg.brokers.pop(key, None)

        config_mod.update(data_dir, drop_config)
    if any(k in vault.load().get("brokers", {}) for k in RETIRED):
        def drop(d: dict) -> None:
            for k in RETIRED:
                d.get("brokers", {}).pop(k, None)

        vault.update(drop)
    for key, label in RETIRED.items():
        if not state.known(key) and not state.runs(key, 1) and not state.unknown_events(key):
            continue
        created = state.forget_broker(key)
        log.info("removed the retired broker %s (%d activities stay in Wealthfolio)", key, created)
        if created:
            state.set_meta(meta_keys.RETIRED_NOTICE(key), json.dumps({"label": label, "activities": created,
                                                     "search": f"[SYNC {key}:"}))


def notices(state: State) -> list[dict]:
    """One per retired broker that left activities in Wealthfolio, until dismissed."""
    out = []
    for key in RETIRED:
        raw = state.meta(meta_keys.RETIRED_NOTICE(key))
        if raw:
            out.append({"key": key, **json.loads(raw)})
    return out


def dismiss(state: State, key: str) -> None:
    state.delete_meta(meta_keys.RETIRED_NOTICE(key))
