"""Any bank over FinTS without a profile of its own (#136): bank code and FinTS address are credentials.

Sparkassen, Volks- und Raiffeisenbanken and most other German banks speak FinTS with PIN/TAN.
The standard posting texts of ``FintsAdapter`` apply; giro bookings of depot trades become
``SECURITIES_CASH`` as for DKB. Holdings come over HKWPD when the bank reports a depot, but
they count as incomplete (no ``reports_positions``) until a bank is confirmed.

A few fixed slots, so a user can set up several banks: every key (config, vault, state,
``[SYNC <key>:…]`` references) stays the same for good. Never remove a slot that may be in
use - ``retired`` would have to clean it up.
"""

from __future__ import annotations

from .base import BrokerAdapter
from .fints import FintsAdapter, credential_fields

SLOTS = 3


class FintsBankAdapter(FintsAdapter):
    credential_fields = credential_fields("deiner Bank", "Online-Banking", blz=True, server=True)


def _slot(n: int) -> type[BrokerAdapter]:
    label = f"Weitere Bank {n} (FinTS)"
    return type(f"FintsBank{n}Adapter", (FintsBankAdapter,),
                {"key": f"fints{n}", "label": label, "default_label": label, "slot_kind": "fints",
                 "__module__": __name__})


BANKS = [_slot(n) for n in range(1, SLOTS + 1)]
