# Banks and Brokers in Detail

What the service fetches from each broker or bank, how it books transactions, and what you need to configure. Back to the [README](../README.md).

## DKB

The service reads your **DKB current account** over FinTS (HBCI), the same interface used by finance applications such as Hibiscus and MoneyMoney. Access is read-only; it cannot initiate transfers.

**Configure** under *Broker → DKB*:

- **Login name and PIN:** the same credentials as DKB online banking.
- **Current-account IBAN:** required only if you have multiple DKB accounts.
- **Accounts:** a DKB cash account and securities account in Wealthfolio; optionally, a **savings account** as well.

**Approval in the DKB app:** DKB requires confirmation on first login and periodically afterwards (usually every 90 days under PSD2 rules). Approve in the app, then click *I have confirmed in the app*. If the scheduled sync is running, the service sends an ntfy notification and waits three minutes for approval.

**Bookings**

| Current-account transaction | Wealthfolio (DKB cash account) |
|---|---|
| Credit, salary, incoming transfer | Deposit (`DEPOSIT`) |
| Card payment, direct debit, outgoing transfer | Withdrawal (`WITHDRAWAL`), i.e. an expense |
| Outgoing transfer to your own account | Transfer (`TRANSFER_OUT`, with a matching entry on the selected account) if a transfer rule matches; see *Everyday use* |
| Credit interest / account fee on statement | Interest (`INTEREST`) / fee (`FEE`) |
| Securities settlement, investment income, dividend | **Not booked here**; matched against the trade, see below |

**Securities:** The service reads the DKB depot's transactions over FinTS (MT536) and books purchases and sales itself, in the same two-account model as a PDF import: a buy or sale on the DKB securities account with its transfer pair to or from the cash account.

| Depot transaction | Wealthfolio |
|---|---|
| Purchase (received against payment) | Buy (`BUY`) with a transfer from the cash account |
| Sale (delivered against payment) | Sell (`SELL`) with a transfer to the cash account |
| Reversal, corporate action, delivery in or out without payment | **Not booked**; listed on *Unknown* to complete by hand |

- **Amount:** a trade gets the amount of the current-account booking that paid it (debit for a purchase, credit for a sale, from one day before to six days after the trade day). That booking is then recognised in the same sync and not booked again. A trade without such a booking waits up to seven days for it; after that it is booked with the amount DKB posted in the depot.
- **Fee:** only when DKB sends a price: the difference between the booking and shares × price. For a sale it holds fees and taxes together. Without a price the fee stays 0 – nothing is guessed.
- **Already imported:** a trade already in Wealthfolio from a PDF or CSV import (same share count and amount, same day) is recognised and not booked again.
- **Older trades:** DKB delivers the last 89 days. A trade from before the first day of the current-account history the service has read gets its money from outside (a deposit before a purchase, a withdrawal after a sale), as its booking was never read.
- **Approval:** if DKB asks for an approval for depot transactions, they are not fetched and nothing is booked from them (a note in the log); a rejected PIN stops everything as usual. A failure reading them never stops the current-account sync.

Securities-related current-account transactions without a matching trade (e.g. a dividend) are still checked against what a PDF import booked (same amount, no more than six days apart); if nothing matches you receive a notification, and after importing the settlement the next sync recognises it. DKB also provides **portfolio holdings** through FinTS. After each sync, the service compares them with the DKB securities account in Wealthfolio. If a position is missing, add it as an opening position on the *Check* page.

**Savings account:** If you select a separate Wealthfolio account for savings in broker settings, the service also reads its transactions and balance. Interest, deposits, and withdrawals are booked to that account. Transfers between the current and savings accounts are booked once as transfers between those accounts, not as a withdrawal and deposit. The service discovers the savings account automatically; if that fails, enter its IBAN with the credentials. On the first sync, savings transactions are fetched from the configured start date.

**History:** If *Import transactions from* has no date, the first sync fetches the last 89 days. DKB requires app approval for older transactions.

**Older securities settlements:** If you import settlements dated before the first day read from the current account, the service has never seen the cash movement. It therefore books a deposit before each such purchase and a withdrawal after each such sale. Dividends remain on the cash account. Purchases and sales from the first day read onwards are covered by the corresponding current-account transaction.

**Balance:** After each sync, the *Brokers* page shows the DKB balance (and the savings-account balance). These should match the corresponding Wealthfolio cash accounts.

**Security:** If DKB rejects the login name or PIN, the service will not retry until you save the credentials again. Three failed attempts can lock DKB online banking.

## Deutsche Bank

The service reads your **Deutsche Bank current account** and **maxblue portfolio holdings** through FinTS (HBCI), read-only. Deutsche Bank no longer requires a separate FinTS activation in online banking.

**Configure** under *Broker → Deutsche Bank*:

- **Login name:** your Deutsche Bank ID, or branch number and account number (10 digits, including leading zeros if needed), as used for online banking; also enter the **PIN**.
- **Bank code:** the code for your account (it depends on the branch; it appears in digits 5–12 of your IBAN).
- **Current-account IBAN:** needed only if you have multiple current accounts. Without it, the service selects a current account, not a savings or securities account; the selected account is shown in the log.
- **Accounts:** a Deutsche Bank cash account and a maxblue securities account in Wealthfolio.

**BestSign approval:** Deutsche Bank requires confirmation in its app (BestSign) on first login and periodically afterwards. Approve in the app and click *I have confirmed in the app*. During the scheduled sync, ntfy sends a notification and the service waits three minutes. With photoTAN, login is only possible in the web UI.

**Bookings:** Current-account transactions are handled as for DKB: deposits, withdrawals, transfers, interest and fees. If the bank does not provide MT940 format, the service fetches camt format instead; the log reports how many transactions came through each route. Current-account transactions related to securities trades (buy, sell, investment income) are not booked directly; they are reconciled against the portfolio data.

**Portfolio:** Holdings are fetched through FinTS and compared with the Wealthfolio securities account after each sync. Deutsche Bank does not provide portfolio transactions through FinTS. Import them using the **CSV export**: in online banking, open Portfolio → Transactions, export as CSV, and upload it under *Broker → Deutsche Bank*. You can upload overlapping date ranges repeatedly; duplicates are avoided. Account and portfolio numbers from the file are not stored.

| CSV export | Wealthfolio |
|---|---|
| Dividend/distribution | Dividend with quantity, net amount and tax (capital gains tax, withholding tax); cash is transferred to the cash account and reconciled against the current-account credit |
| Buy/sell | Buy/sell with quantity, fees and tax; cash transfer from/to the cash account |
| Incoming position with amount (e.g. employee shares) | Buy; because the cash did not pass through the current account, a deposit of the same amount is booked beforehand |
| Buys/sells before the first current-account date read | As above, but with a deposit before a purchase or withdrawal after a sale; the corresponding current-account transaction was never read. Dividends always remain on the cash account |
| Capital transaction, outgoing/incoming without amount | **Not booked**; shown under *Unknown bookings*. Add corporate actions (e.g. a spin-off) and portfolio transfers as opening positions on *Check* |

**History and security:** As for DKB: the default is the last 89 days; after a rejected PIN, the service does not retry until you save the credentials again.

## Other banks (FinTS)

Most German banks—including Sparkassen, Volksbanken and Raiffeisenbanken, Sparda and PSD banks, and many direct banks—offer FinTS with PIN/TAN. Configure up to three under *Broker → Other bank 1 (FinTS)*, *Other bank 2*, and *Other bank 3*.

- **Login name and PIN:** as in your bank's online banking.
- **Bank code:** your account's eight-digit bank code.
- **FinTS URL:** leave blank by default. The service knows the URLs in the German Banking Industry's list of FinTS-capable banks. If your bank is missing, enter its URL (must begin with `https://`; look on the bank's website under *FinTS* or *HBCI*). Your credentials are sent only to this URL.
- **IBAN and accounts:** configure as for DKB.

Transactions are booked as for DKB: expenses as withdrawals, incoming funds as deposits, and interest and fees separately. Current-account transactions related to securities (descriptions containing *securities*, *portfolio*, *dividend*, etc.) are held back until the corresponding securities-side transaction exists in Wealthfolio, for example after importing settlement statements. If the bank reports a portfolio, the service reads and reconciles its holdings; the portfolio is considered complete only after the bank confirms it. If your bank uses different booking descriptions or something fails, open an issue so a dedicated profile can be added.

## Banks via PSD2 (e.g. N26)

Every EU bank must provide account data through an interface under the Payment Services Directive (PSD2), including banks without FinTS such as N26. Only authorised account-information service providers may read this data. The service uses **Enable Banking**: **your account data passes through Enable Banking**, an authorised provider based in Finland, rather than directly from the bank to the service. If you do not want this, use FinTS or import files instead.

**Create an Enable Banking app (one-time setup):**

1. Create an account in the Enable Banking console and register an app for production use. Without a contract, it runs in restricted mode: free, but only for your own linked accounts.
2. Set the **redirect URL** to this service with any path, e.g. `https://<service-address>:8443/psd2`.
3. Keep the generated **private key** (`.pem` file).
4. Link your bank account to the app as described in the console.

**Configure** under *Broker → Bank via PSD2 1* (up to three banks: *2* and *3*):

- **Bank:** the name used by Enable Banking, e.g. `N26`. If the name is misspelled, the service suggests similar names.
- **Country:** leave blank for Germany; otherwise enter the country code.
- **Application ID** and **private key:** from steps 1 and 3; the key is stored encrypted in the vault.
- **IBAN:** required only if the bank exposes multiple accounts; otherwise the current account is selected.

**Login:** The service displays a link to your bank. Sign in there and approve access. You are then redirected to the redirect URL; the page itself does not need to load. Copy the URL from the browser address bar into the service. Approval lasts as long as the bank permits (usually 90 or 180 days); ntfy reminds you seven days and one day before expiry.

**Bookings:** As for DKB: expenses as withdrawals, incoming funds as deposits, interest and fees separately; pending transactions are only imported once booked. PSD2 covers payment accounts, **not securities portfolios**. Current-account transactions related to securities are held until the corresponding securities-side transaction exists in Wealthfolio.

## N26

> **Important:** N26 does not offer an official interface for retail customers. The service uses the interface used by the N26 app, as other finance tools do. N26 may change it or prohibit such access at any time, in which case syncing will stop. You must acknowledge this notice once in broker settings before login can start.

**Configure** under *Broker → N26*: your N26 email address and password.

**Login:** The service signs in and N26 sends a confirmation request to the N26 app. Approve it and click *I have confirmed in the app*. The service can then sync without you while N26 keeps the session valid. When it expires, ntfy reports *Login required*. A new request in the app only starts the UI flow; the scheduled sync never triggers one by itself. If N26 rejects the password or email, the service will not retry until you save the credentials again.

**Bookings:** Incoming funds are deposits; card payments, direct debits and transfers are withdrawals. Pending transactions are imported only after booking. Moving money into an N26 Space appears as a withdrawal from the main account; a transfer rule on the *Transfers* page (e.g. a Space keyword) turns it into a transfer to a dedicated Wealthfolio account. The main-account balance is reconciled after each sync.

## Trade Republic

The service reads your Trade Republic timeline through the app's unofficial interface, using the open-source project [pytr](https://github.com/pytr-org/pytr). Access is read-only; it cannot place orders or initiate withdrawals.

> **Important:** Trade Republic has no official API. The interface used here may change at any time, and syncing may stop until the service is updated. Automated access may not be intended by Trade Republic. If you prefer not to use it, continue importing transactions via CSV. You must acknowledge this notice once in broker settings before login can start.

**Configure** under *Broker → Trade Republic*:

- **Phone number** including country code (e.g. `+4917612345678`) and the app **PIN**.
- **Accounts:** a Trade Republic cash account and securities account in Wealthfolio.

**Login:** Login works similarly to the browser at app.traderepublic.com. Trade Republic sends a request to the app; approve it and click *I have confirmed in the app*. The mobile app remains logged in. If your account uses an authenticator app, enter its code on the page. The session lasts for a while; when it expires, the scheduled sync sends an ntfy notification and waits two minutes for approval in the app.

**Bookings**

| Timeline event | Wealthfolio |
|---|---|
| Buy, savings plan, sell | Buy/sell in the securities account, with fees and tax in separate fields; cash transferred from/to the cash account |
| Dividend/distribution | Dividend with net amount and withholding tax; cash transferred to the cash account |
| Saveback | Bonus (`CREDIT/BONUS`) in the securities account that funds the purchase, without debiting the cash account |
| Interest | Interest (`INTEREST`) with tax |
| Advance lump-sum tax / tax correction | Tax (`TAX`) / tax refund (`CREDIT/TAX_REFUND`); a correction that debits cash is booked as tax |
| Referral bonus | Bonus (`CREDIT/BONUS`) in the cash account |
| Worthless position removed | Sale at zero in the securities account, with no cash movement |
| Deposit, card refund | Deposit (`DEPOSIT`) |
| Card payment, transfer | Withdrawal (`WITHDRAWAL`) or transfer to your own account if a transfer rule matches |
| Dividend reinvestment | Buy of the new shares in the securities account, paid from the cash account; the share count comes from the event's details - without it the event is reported as unknown. The cash leg of the reinvestment is reported as unknown |
| Stock split, spin-off, exchange, portfolio transfer, private markets | **Not booked**; reported as unknown. Book manually or import via CSV |

Informational events (order created/cancelled, documents, address change, etc.) and cancelled transactions are ignored.

**CSV imports alongside the service:** The service recognises transactions already imported from CSV and does not duplicate them: same type and amount, no more than 36 hours apart, and the same quantity for buys and sells. The security may use a different symbol (the imported ticker versus the ISIN used by the service).

## Scalable Capital

The service reads your Scalable portfolio using **Scalable's official command-line tool**, [Scalable CLI](https://github.com/ScalableCapital/scalable-cli) (`sc`). This is an interface provided by Scalable itself, not a reverse-engineered web app. Installation and updates download `sc` from Scalable's GitHub release and verify Scalable's signature and checksum first. The service uses read-only commands and logs in with `--local-read-only`; it cannot place orders or savings plans. `sc` runs sandboxed with Landlock: it cannot see the service key or data, and receives only the language setting from the service environment.

> **Important:** Scalable says the CLI is in beta and unsupported. If Scalable changes it, syncing may stop until the service is updated.

**One-time setup in Scalable:** In the **web portal**, enable Scalable CLI under *Profile → Security → Agentic Investing*. Without this, Scalable rejects the login and the service explains why.

**Configure** under *Broker → Scalable Capital*:

- **Portfolio ID:** required only if you have multiple Scalable portfolios; use the `portfolioId` in the portfolio page URL. Leave blank if you have one.
- **Accounts:** a Scalable cash account and securities account in Wealthfolio.

**Login:** The page shows a link and code. Open the link, sign in to Scalable, approve the code, then click *I have confirmed*. The session refreshes on each sync and is stored encrypted in the service vault. If Scalable requires a new login, the scheduled sync sends the link and code via ntfy and waits ten minutes for approval.

**Bookings**

| Scalable event | Wealthfolio |
|---|---|
| Buy, savings plan, sell | Buy/sell in the securities account, with order, venue and crypto spread fees and tax in separate fields; cash transferred from/to the cash account |
| Distribution | Dividend with net amount and tax if Scalable provides it; quantity is 1 because the interface does not provide one. Cash transferred to the cash account |
| Interest | Interest (`INTEREST`) |
| Tax (e.g. advance lump-sum tax) / tax refund | Tax (`TAX`) / tax refund (`CREDIT/TAX_REFUND`) |
| Fee (e.g. PRIME+) | Fee (`FEE`) |
| Deposit / withdrawal | Deposit (`DEPOSIT`) / withdrawal (`WITHDRAWAL`) or transfer to your own account if a rule matches |
| Portfolio transfer (securities removed and re-added; cash marked `SWITCH-`) | **Not booked**; the entries cancel each other out |
| Fund exchange (securities and `SWAP_OUT` credit on the same day) | Sale at the credited amount |
| Certificate repayment/knock-out (position removed at zero and distribution on the same day) | Sale at the paid-out amount |
| Distribution reversal | Reversal and original distribution cancel each other out. If the service had already imported it, *Check* shows it as “no longer at Scalable”; delete it in Wealthfolio |
| Other reversals, individual securities transfers, ELTIFs, negative interest, fee refunds | **Not booked**; reported as unknown for manual review |

Open orders are imported only after execution. The first full-history sync takes a few minutes because Scalable rate-limits details for buys and sells. If necessary, the service books what it has and fetches the remainder on the next run. If nothing arrives, the log (`journalctl -u wealthfolio-companion -n 50`) reports counts by transaction type, without amounts or names. CSV imports are deduplicated as for Trade Republic. Scalable's CSV export does not include dividend tax; when the service recognises an imported dividend, it fills in the tax from Scalable's data (only the tax field changes; amount and comment remain unchanged).

## comdirect

The service reads both your current account **and securities portfolio** through comdirect's **official retail REST API**, read-only; it never calls order or transfer endpoints.

**One-time setup at comdirect:** In comdirect online banking, open *Personal area → Manage access* and enable the **REST API**. You will receive a **Client ID** and **Client Secret**. **photoTAN Push** is recommended, so you can approve logins in the photoTAN app.

**Configure** under *Broker → comdirect*: access number, PIN, Client ID, Client Secret; enter the IBAN only if you have multiple current accounts. Configure a comdirect cash account and securities account in Wealthfolio.

**Only the portfolio is booked, not the current account.** The comdirect cash account in Wealthfolio contains dividends only: before each purchase, the service books a deposit equal to the purchase amount; after each sale, it books a withdrawal equal to the proceeds. Current-account transfers, card payments, fees and interest are not imported, and the current-account balance is not reconciled.

**Securities held before the start date** appear on *Check* under “Holding without purchase in Wealthfolio”. Enter the purchase date, unit price (pre-filled with comdirect's cost basis) and fees. The next sync books the purchase and corresponding deposit.

**Login:** Approve photoTAN Push in the app and click *I have confirmed in the app* (for photoTAN graphics or mobileTAN, enter the TAN). comdirect sessions are short-lived: the access token lasts ten minutes and can be renewed while the service uses it. The scheduled sync therefore usually needs **fresh approval**: it sends an ntfy notification and waits three minutes for approval in the app. TANs that must be typed can only be entered in the web UI.

> **Lockout protection:** comdirect locks online banking after **five TAN requests without a correct TAN** or **three incorrect TANs**. The service stops requesting further TANs after three unanswered requests or two incorrect TANs. Then sign in once at comdirect.de using a TAN and save the credentials in the service again.

**Bookings**

| comdirect event | Wealthfolio |
|---|---|
| Buy/sell (portfolio transaction) | Buy/sell in the securities account with quantity and market value; amount is taken from the related securities transaction on the current account, with the difference treated as a fee. The API does not separate fees and tax on sales, so both are included in the fee. A deposit (buy) or withdrawal (sell) is booked on the cash account, with a transfer between accounts in between |
| Dividend/income (interest/dividend; security identified by ISIN or WKN in description) | Dividend with net amount in the securities account; cash transferred to the cash account. The tax (withholding tax plus capital gains tax, solidarity surcharge and church tax) is added only when the description names the gross amount and each tax and they add up to the net amount - foreign amounts converted at the exchange rate named there. Otherwise the dividend is booked net without tax; nothing is estimated. Dividends booked by an earlier version get their tax added once (only the tax field changes) |
| Dividend whose security cannot be identified | **Not booked**; reported as unknown |
| All other current-account transactions (transfers, card payments, fees, interest, etc.) | **Not booked** |
| Portfolio transfer, or buy/sell with no matching cash transaction after 10 days | **Not booked**; reported as unknown. Add the holding under *Check* |

Pending transactions and orders are imported only after they are booked. Without a start date, the service looks back ten years (comdirect provides only six months if no date is specified) and imports what comdirect makes available. The oldest dates are recorded in the log (`comdirect: … giro bookings (oldest …), … depot transactions (oldest …)`).
