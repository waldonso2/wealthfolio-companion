# Spending tracker: test on a real container (comdirect)

Goal: the comdirect cash account (giro) appears in Wealthfolio's spending tracker, the depot does not, and
switching off removes only what the service added. Use your own accounts; write nothing down from them here.

## Before
1. Wealthfolio is reachable and the comdirect broker is set up with a cash account and a depot account.
2. In Wealthfolio, open the spending tracker and note which accounts it tracks and whether it is on.
   Track one unrelated account yourself if you want to check that it survives.
3. Wealthfolio must offer the spending tracker (a version without it ends in "not updated" in the log, nothing else).

## Turn on
1. Service page *Spending tracker* (`/services/spending`): pick the comdirect cash account, tick the confirmation
   (switching deletes the service's earlier comdirect bookings and books everything again), enter your password
   and save. The result line says how many bookings were deleted.
   Then fetch comdirect (*Fetch now*, approve the TAN): the whole current account is booked from the start date.
2. *Services*: switch the service on, then *Run now* (or wait for the next run).
3. In Wealthfolio's spending tracker check:
   - the tracker is on;
   - the comdirect **cash** account is tracked, the **depot** is not;
   - your own account from step 2 is still tracked;
   - card payments, direct debits and transfers of the current account are there, categorised by Wealthfolio's rules;
   - dividends show up as income; a BUY or SELL is a transfer between cash account and depot, not spending;
   - one *Anfangsbestand* deposit sits before your first booking (it brings the cash account to the real balance,
     so Wealthfolio may count it as income in that month - add a rule for it if you want it out of the report);
   - the cash account's balance in Wealthfolio is close to the current account's balance at comdirect (pending
     bookings are not in it).
4. Run again: nothing changes (same accounts, no duplicate entry).
5. The log has no "spending tracker not updated".

## Turn off
1. Switch the service off on *Services* and run (or clear the broker's choice on `/services/spending`, with the
   confirmation and your password: the service then deletes its comdirect bookings and the next fetch books only the
   portfolio again, as before the tracker).
2. In Wealthfolio check:
   - the comdirect cash account is no longer tracked;
   - your own account is still tracked;
   - the tracker's own on/off switch is as you left it;
   - after clearing the broker's choice: only the portfolio model is booked again after the next fetch;
   - switching the service off on *Services* alone deletes nothing (the booking model changes only with the choice
     on `/services/spending`).
3. An account you tracked in Wealthfolio before turning the service on stays even after the service is off.

## If something is off
- Account missing: is the cash account set on the broker and chosen on `/services/spending`? Is the service on?
- Tracker stays off: look for "spending tracker not updated" in the log (old Wealthfolio or a rejected request).
- The sync's result for comdirect never depends on this step; a failure here only shows in the log.

## Good to know

- While the *Spending tracker* service is on and a broker's cash account is selected, every run makes sure Wealthfolio's tracker is switched on. If you switch the tracker off in Wealthfolio on purpose, switch the service off here too, otherwise the next run switches it on again.
- Switching the service off removes only the accounts this service added and never touches the tracker's own switch.
