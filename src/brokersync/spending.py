"""Wealthfolio's spending tracker for the banks it is turned on for (#134, ``BrokerConfig.spending``).

The tracker works per account and categorizes by Wealthfolio's own rules on the activity's note - the
comment the sync writes already names counterparty and purpose, so nothing of an activity changes here.
After each run the cash accounts of those banks are put into the tracked set; one the sync put there
(``meta`` ``spending``) leaves it again when its toggle is off. Accounts the user chose in Wealthfolio
are never removed. Own transfers and securities settlements are linked pairs, which the tracker
counts as saving or neutral, never as spending.
"""

from __future__ import annotations

import json

from . import meta_keys
from .config import Config
from .state import State
from .wealthfolio import WealthfolioClient

KEY = meta_keys.SPENDING()


def update(state: State, wf: WealthfolioClient, cfg: Config) -> None:
    wanted = {b.cash_account_id for b in cfg.brokers.values() if b.spending and b.cash_account_id}
    ours = set(json.loads(state.meta(KEY) or "[]"))
    if not wanted and not ours:
        return  # never asks Wealthfolio while nobody turned it on
    settings = wf.spending_settings()
    tracked = settings.get("accountIds") or []
    keep = [a for a in tracked if a in wanted or a not in ours]
    new = keep + sorted(wanted - set(keep))
    # The tracker is also turned on when every wanted account was tracked already but the tracker is off.
    if new != tracked or (wanted and not settings.get("enabled")):
        wf.set_spending_accounts(new, enable=bool(wanted))
    # Only what the sync added counts as ours; an account the user tracked before stays theirs.
    state.set_meta(KEY, json.dumps(sorted((ours & wanted) | (wanted - set(tracked)))))
