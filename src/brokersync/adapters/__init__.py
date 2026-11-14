"""Registry of broker adapters. A new broker is one module plus one line here."""

from __future__ import annotations

from .base import AdapterError, AuthRequired, BrokerAdapter, Challenge, CredentialField
from .comdirect import ComdirectAdapter
from .deutschebank import DeutscheBankAdapter
from .dkb import DkbAdapter
from .enablebanking import BANKS as PSD2_BANKS
from .fintsbank import BANKS as FINTS_BANKS
from .n26 import N26Adapter
from .scalable import ScalableAdapter
from .tr import TradeRepublicAdapter

ADAPTERS: dict[str, type[BrokerAdapter]] = {
    DkbAdapter.key: DkbAdapter,
    TradeRepublicAdapter.key: TradeRepublicAdapter,
    ScalableAdapter.key: ScalableAdapter,
    ComdirectAdapter.key: ComdirectAdapter,
    DeutscheBankAdapter.key: DeutscheBankAdapter,
    N26Adapter.key: N26Adapter,
    **{a.key: a for a in FINTS_BANKS},
    **{a.key: a for a in PSD2_BANKS},
}

SLOT_KINDS = {"fints": FINTS_BANKS, "psd2": PSD2_BANKS}


def apply_labels(adapters: dict[str, type[BrokerAdapter]], brokers: dict) -> None:
    """A bank in a fixed slot is shown under the name the user gave it (``BrokerConfig.label``). The slot classes
    are their own, so setting the label on one never touches another."""
    for key, adapter in adapters.items():
        default = getattr(adapter, "default_label", None)
        if default is not None:
            adapter.label = (brokers[key].label if key in brokers and brokers[key].label else default)


__all__ = [
    "ADAPTERS",
    "SLOT_KINDS",
    "AdapterError",
    "AuthRequired",
    "BrokerAdapter",
    "Challenge",
    "CredentialField",
    "apply_labels",
]
