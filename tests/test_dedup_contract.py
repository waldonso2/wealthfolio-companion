"""The duplicate-detection cases shared with the Broker Importer addon (contract/dedup/cases.json): the sync and
the addon must recognise the same activities as already in Wealthfolio."""

import json
from pathlib import Path

import pytest

from brokersync.dedup import ExistingIndex

CASES = json.loads((Path(__file__).parent.parent / "contract" / "dedup" / "cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_shared_dedup_case(case):
    index = ExistingIndex([dict(e) for e in case["existing"]])
    got = {}
    for a in case["incoming"]:
        payload = {"accountId": a["accountId"], "activityType": a["activityType"], "subtype": a.get("subtype"),
                   "activityDate": a["date"], "quantity": a.get("quantity"), "amount": a["amount"],
                   "currency": a.get("currency")}
        if a.get("symbol"):
            payload["asset"] = {"symbol": a["symbol"]}
        got[str(a["line"])] = index.find(f"[SYNC test:{a['line']}]", [payload])
    assert got == case["expect"]


def test_index_sees_activities_appended_after_a_find_and_keeps_the_window_edge():
    from brokersync.dedup import MATCH_WINDOW_SECONDS, ExistingIndex

    def dep(i, date, amount):
        return {"id": f"e{i}", "accountId": "c", "activityType": "DEPOSIT", "date": date, "amount": amount,
                "currency": "EUR", "comment": "import"}

    def payload(date, amount):
        return [{"accountId": "c", "activityType": "DEPOSIT", "activityDate": date, "amount": amount,
                 "currency": "EUR"}]

    index = ExistingIndex([dep(1, "2026-01-01T00:00:00Z", "10")])
    assert index.find("[SYNC x:1]", payload("2026-03-01T00:00:00Z", "10")) is None
    # The sync appends the legs it created during a run; the next find sees them.
    index.existing.append(dep(2, "2026-03-01T01:00:00Z", "10"))
    assert index.find("[SYNC x:2]", payload("2026-03-01T00:00:00Z", "10")) == "e2"
    # Exactly 36 hours away still matches, a second more doesn't.
    assert MATCH_WINDOW_SECONDS == 36 * 3600
    index.existing += [dep(3, "2026-05-02T12:00:00Z", "20"), dep(4, "2026-05-10T12:00:01Z", "30")]
    assert index.find("[SYNC x:3]", payload("2026-05-01T00:00:00Z", "20")) == "e3"
    assert index.find("[SYNC x:4]", payload("2026-05-09T00:00:00Z", "30")) is None


def test_another_transactions_activity_of_the_sync_is_never_a_match():
    """Two equal card payments within the window (#134 H1): the second is its own booking, not the first."""
    first = {"id": "s1", "accountId": "c", "activityType": "WITHDRAWAL", "date": "2026-03-01T08:00:00Z",
             "amount": "4.50", "currency": "EUR", "comment": "Bäckerei [SYNC x:1]"}
    index = ExistingIndex([first])
    payload = [{"accountId": "c", "activityType": "WITHDRAWAL", "activityDate": "2026-03-01T09:00:00Z",
                "amount": "4.50", "currency": "EUR"}]
    assert index.find("[SYNC x:2]", payload) is None
    index.existing.append({**first, "id": "i1", "comment": "Bäckerei"})  # an import of it still matches
    assert index.find("[SYNC x:2]", payload) == "i1"


def test_activities_with_the_reference_are_used_also_when_they_come_after_a_find():
    def act(i, comment):
        return {"id": i, "accountId": "c", "activityType": "DEPOSIT", "date": "2026-03-01T08:00:00Z",
                "amount": "5", "currency": "EUR", "comment": comment}

    payload = [{"accountId": "c", "activityType": "DEPOSIT", "activityDate": "2026-03-01T09:00:00Z", "amount": "5",
                "currency": "EUR"}]
    index = ExistingIndex([act("a", "import [SYNC x:1]")])
    assert index.find("[SYNC x:1]", payload) is None and index.used == {"a"}
    index.existing.append(act("b", "leg [SYNC x:2]"))  # the sync appends what it creates during a run
    assert index.find("[SYNC x:2]", payload) is None and index.used == {"a", "b"}
    assert index.find("[SYNC x:3]", payload) is None and index.used == {"a", "b"}  # no such reference: nothing new


def test_settlement_matching_uses_direction_and_each_activity_only_once():
    from datetime import UTC, datetime
    from decimal import Decimal

    from brokersync.dedup import ExistingIndex

    when = datetime(2026, 3, 1, 12, tzinfo=UTC)
    index = ExistingIndex([
        {"id": "out", "accountId": "cash", "activityType": "TRANSFER_OUT",
         "date": "2026-03-01T12:00:00Z", "amount": "125.00", "comment": ""},
        {"id": "in", "accountId": "cash", "activityType": "TRANSFER_IN",
         "date": "2026-03-01T12:00:00Z", "amount": "125.00", "comment": ""},
    ])

    assert index.find_settlement("cash", Decimal("-125.00"), when, 2) == "out"
    assert index.find_settlement("cash", Decimal("-125.00"), when, 2) is None
    assert index.find_settlement("cash", Decimal("125.00"), when, 2) == "in"
    assert index.find_settlement("cash", Decimal("125.00"), when, 2) is None


def test_settlement_matching_rejects_wrong_account_amount_and_date_window():
    from datetime import UTC, datetime
    from decimal import Decimal

    from brokersync.dedup import ExistingIndex

    when = datetime(2026, 3, 1, 12, tzinfo=UTC)
    index = ExistingIndex([
        {"id": "late", "accountId": "cash", "activityType": "TRANSFER_OUT",
         "date": "2026-03-04T12:00:00Z", "amount": "125.00", "comment": ""},
        {"id": "amount", "accountId": "cash", "activityType": "TRANSFER_OUT",
         "date": "2026-03-01T12:00:00Z", "amount": "124.00", "comment": ""},
        {"id": "account", "accountId": "other-cash", "activityType": "TRANSFER_OUT",
         "date": "2026-03-01T12:00:00Z", "amount": "125.00", "comment": ""},
    ])

    assert index.find_settlement("cash", Decimal("-125.00"), when, 2) is None
