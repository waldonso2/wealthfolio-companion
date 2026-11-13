# Spending tracker: test on a real container (comdirect)

Goal: the comdirect cash account (giro) appears in Wealthfolio's spending tracker, the depot does not, and
switching off removes only what the service added. Use your own accounts; write nothing down from them here.

## Before
1. Wealthfolio is reachable and the comdirect broker is set up with a cash account and a depot account.
2. In Wealthfolio, open the spending tracker and note which accounts it tracks and whether it is on.
   Track one unrelated account yourself if you want to check that it survives.
3. Wealthfolio must offer the spending tracker (a version without it ends in "not updated" in the log, nothing else).

## Turn on
1. Service page *Ausgaben-Tracker* (`/services/spending`): pick the comdirect cash account, save.
2. *Dienste*: switch the service on, then *Jetzt ausführen* (or wait for the next run).
3. In Wealthfolio's spending tracker check:
   - the tracker is on;
   - the comdirect **cash** account is tracked, the **depot** is not;
   - your own account from step 2 is still tracked;
   - dividends booked on the cash account show up as income, trades are not spending
     (comdirect books only the depot: a BUY is a deposit plus the trade, a SELL the trade plus a withdrawal).
4. Run again: nothing changes (same accounts, no duplicate entry).
5. The log has no "spending tracker not updated".

## Turn off
1. Switch the service off on *Dienste* and run (or clear the broker's choice on `/services/spending`).
2. In Wealthfolio check:
   - the comdirect cash account is no longer tracked;
   - your own account is still tracked;
   - the tracker's own on/off switch is as you left it;
   - no activity changed, nothing was deleted.
3. An account you tracked in Wealthfolio before turning the service on stays even after the service is off.

## If something is off
- Account missing: is the cash account set on the broker and chosen on `/services/spending`? Is the service on?
- Tracker stays off: look for "spending tracker not updated" in the log (old Wealthfolio or a rejected request).
- The sync's result for comdirect never depends on this step; a failure here only shows in the log.
