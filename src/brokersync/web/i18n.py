"""The UI's languages (#87): German, and English as the second one.

gettext style with the German text as the key: a template writes ``{{ _("Speichern") }}``, Python code
``tr(request, "Gespeichert.")`` (``web/context.py``) or ``translate(text, lang)``. German needs no catalogue -
the key is the text -, so the German pages stay exactly as they were. ``EN`` maps every key to its English
text; a key without an entry shows the German text. Dynamic parts are ``str.format`` placeholders
(``"{n} Buchungen gelöscht."``) filled after the lookup, so a value (a broker's name, an amount, a bank's
error text) is never looked up or translated itself.

Plurals: one key per form, the caller picks it - ``translate("{n} Buchung" if n == 1 else "{n} Buchungen",
lang, n=n)``; the completeness test (``tests/test_i18n.py``) reads both branches.

Texts the code holds before it knows the language (``STATUS_LABEL``, ``SETTINGS_PAGES``, the checks on
*Sicherheit*) are marked with ``N_`` - it returns the text unchanged and only tells the completeness test
that it is a key - and translated where they are shown (``_(label)`` in the template).

Not translated, on purpose: ntfy notifications, texts from adapters (challenge messages, ``AdapterError`` -
often the bank's own wording), foreign error texts (Wealthfolio, banks), log lines, comments of activities in
Wealthfolio (part of the duplicate fingerprint) and the GitHub report of an unknown booking.
"""

from __future__ import annotations

from jinja2 import pass_context
from markupsafe import Markup

LANGUAGES = ("de", "en")


def N_(text: str) -> str:  # gettext's name for "a key, translated where it is shown"
    return text


def lookup(text: str, lang: str | None) -> str:
    """The text in ``lang`` (the German key itself for German, an unknown language or a missing entry)."""
    if lang == "en":
        return EN.get(text, text)
    return text


def translate(text: str, lang: str | None, **params) -> str:
    """``text`` (a German key) in ``lang``, its ``{placeholders}`` filled with ``params``."""
    out = lookup(text, lang)
    return out.format(**params) if params else out


def markup(text: str, lang: str | None, **params) -> Markup:
    """For templates: the key may hold our own inline HTML (``<strong>``, ``<a>``), the params are escaped."""
    out = Markup(lookup(text, lang))  # noqa: S704 - keys and EN entries are our own source
    return out.format(**params) if params else out


@pass_context
def template_gettext(context, text: str, **params) -> Markup:
    """The templates' ``_``: in the language of the page (``lang`` in its context; German without one)."""
    return markup(text, context.get("lang"), **params)


# German key -> English text. Terms: Verrechnungskonto = cash account, Depotkonto = securities account,
# Abruf = fetch, Buchung = booking, Anmeldung = login, Anfangsbestand = opening position, Tresor = vault.
EN: dict[str, str] = {
    # ── navigation, frame ──
    "X-Ray in Wealthfolio": "X-Ray in Wealthfolio",
    "Fehler: {error}": "Error: {error}",
    "legt die Fondslisten für das X-Ray-Addon in Wealthfolio ab; Beträge bleiben in Wealthfolio":
        "stores the funds' lists for the X-Ray addon in Wealthfolio; amounts stay in Wealthfolio",
    "Zuletzt abgelegt: {when}": "Last stored: {when}",
    "Als Secret des Containers außerhalb von data/ (kein TPM): Wer das Volume und das Secret kopiert, kann die "
    "Daten öffnen - dagegen hilft die Master-Passphrase.":
        "As the container's secret outside data/ (no TPM): whoever copies the volume and the secret can open "
        "the data - the master passphrase protects against that.",
    "Dieser Dienst läuft als Docker-Container: Eine neue Version ist ein neues Image. Zum Aktualisieren "
    "auf dem Rechner mit dem Container ./pull-verified-image.sh --env .env &lt;Version&gt; ausführen "
    "(prüft Signatur und Herkunft des Images), danach docker compose pull companion und docker compose "
    "up -d - die Daten bleiben im Volume, beim Start werden sie auf den neuen Stand gebracht.":
        "This service runs as a Docker container: a new version is a new image. To update, run "
        "./pull-verified-image.sh --env .env &lt;version&gt; on the computer with the container (it checks "
        "the image's signature and provenance), then docker compose pull companion and docker compose up -d "
        "- the data stays in the volume and is brought up to date at the start.",
    "Installiert ist {current}. Zum Aktualisieren das neue Image prüfen: <code>./pull-verified-image.sh "
    "--env .env {new}</code>, dann <code>docker compose pull companion</code> und <code>docker compose "
    "up -d</code>.":
        "Installed is {current}. To update, check the new image: <code>./pull-verified-image.sh --env .env "
        "{new}</code>, then <code>docker compose pull companion</code> and <code>docker compose up -d</code>.",
    # Page Dienste (#145)
    "Die Anmeldung läuft gerade schon - bitte kurz warten.": "The login is already in progress - please wait a moment.",
    "Dienste": "Services",
    "Was der Service in dein Wealthfolio schreibt. Jeder Dienst lässt sich einzeln ein- und ausschalten.":
        "What the service writes into your Wealthfolio. Each service can be switched on and off on its own.",
    "schaltet sich ein, sobald die Einstellungen gespeichert sind": "switches on once the settings are saved",
    "Einstellungen: {name}": "Settings: {name}",
    "Broker-Sync": "Broker sync",
    "bucht die Umsätze deiner Broker und Banken in Wealthfolio":
        "books the transactions of your brokers and banks in Wealthfolio",
    "Länder & Sektoren": "Countries & sectors",
    "trägt Länder und Sektoren der ETFs in Wealthfolio ein; deine eigenen Zuordnungen bleiben":
        "enters the ETFs' countries and sectors in Wealthfolio; your own assignments stay",
    "ISIN ergänzen": "Complete ISINs",
    "trägt bei Wertpapieren ohne ISIN die ISIN in Wealthfolio ein":
        "enters the ISIN in Wealthfolio for securities without one",
    "setzt die Logos der Wertpapiere von logo.dev in Wealthfolio":
        "sets the securities' logos from logo.dev in Wealthfolio",
    "Ausgaben-Tracker": "Spending tracker",
    "nimmt die Verrechnungskonten deiner Banken in Wealthfolios Ausgaben-Auswertung auf":
        "adds your banks' cash accounts to Wealthfolio's spending tracker",
    "{name} ist eingeschaltet.": "{name} is on.",
    "{name} ist ausgeschaltet.": "{name} is off.",
    "{what} Danach schaltet sich {name} ein.": "{what} {name} then switches on.",
    "Erst Wealthfolio verbinden.": "Connect Wealthfolio first.",
    "Erst einen Broker einrichten.": "Set up a broker first.",
    "Unbekannter Dienst.": "Unknown service.",
    "Broker-Sync ist ausgeschaltet - auf der Seite Dienste einschalten.":
        "Broker sync is off - switch it on on the page Services.",
    "Broker-Sync ist ausgeschaltet: kein Broker wird abgerufen.": "Broker sync is off: no broker is fetched.",
    "Auf der Seite Dienste einschalten": "Switch it on on the page Services",
    "Übersicht": "Overview",
    "Prüfung": "Check",
    "Unbekannt": "Unknown",
    "Einstellungen": "Settings",
    "Hauptmenü": "Main menu",
    "Abmelden": "Log out",
    "{n} offen": "{n} open",
    "Version {version} · alle Daten verschlüsselt · nur lesender Zugriff auf die Broker":
        "Version {version} · all data encrypted · read-only access to the brokers",
    "Einstellungen {label}": "Settings {label}",
    # ── status labels, settings pages, appearance ──
    "OK": "OK",
    "Anmeldung nötig": "Login needed",
    "Fehler": "Error",
    "Stückzahl": "Shares",
    "läuft": "running",
    "abgebrochen": "aborted",
    "Abruflimit": "rate limited",
    "Abruflimit beim Anbieter": "The provider limits the requests",
    "Abruflimit beim Anbieter - nächster Versuch ab {time}": "The provider limits the requests - next try from {time}",
    "Wealthfolio": "Wealthfolio",
    "Adresse und Passwort deines Wealthfolio": "Address and password of your Wealthfolio",
    "Broker": "Brokers",
    "Zugangsdaten, Konten, Startdatum, Anmeldung": "Credentials, accounts, start date, login",
    "Benachrichtigungen": "Notifications",
    "ntfy für TAN-Anfragen und Fehler": "ntfy for TAN requests and errors",
    "Wertpapiere": "Securities",
    "ISIN unter eigenem Symbol führen": "Keep an ISIN under a symbol of your own",
    "Überträge": "Transfers",
    "Auszahlungen auf eigene Konten erkennen": "Recognise withdrawals to your own accounts",
    "Logos": "Logos",
    "Sicherheit": "Security",
    "Abrufmodus, Verschlüsselung, Master-Passphrase, Zertifikat":
        "Fetch mode, encryption, master passphrase, certificate",
    "Darstellung": "Appearance",
    "Duplikate": "Duplicates",
    "Doppelte Buchungen finden und entfernen": "Find and remove duplicate bookings",
    "Sprache": "Language",
    "Diese Anmeldung wartet bis {when}. Danach oder wenn keine Anfrage kam:":
        "This login waits until {when}. After that, or if no request arrived:",
    "Anmeldung neu starten": "Start the login again",
    # Stored by state.abort_stale_runs, shown through the filter ``known``.
    "Abgebrochen - der Dienst wurde während des Abrufs beendet (Neustart, Update oder Absturz).":
        "Aborted - the service was stopped during the fetch (restart, update or crash).",
    "Hell/Dunkel umschalten": "Switch light/dark",
    # Settings layout and the pages Darstellung / Über
    "Allgemein": "General",
    "Verbindung": "Connection",
    "Daten": "Data",
    "Über": "About",
    "Schrift, Theme und Sprache": "Font, theme and language",
    "Version, Links, Datenschutz": "Version, links, privacy",
    "Schrift, Farben und Sprache dieser Oberfläche.": "Font, colours and language of this interface.",
    "Schrift": "Font",
    "Theme": "Theme",
    "Wie Wealthfolio: die Schrift, die dort unter Einstellungen → Darstellung gewählt ist.":
        "As Wealthfolio: the font chosen there under Settings → Appearance.",
    "Wie Wealthfolio ({font})": "As Wealthfolio ({font})",
    "System folgt dem Gerät. Das Symbol oben kehrt die Farben bis zum Abmelden um.":
        "System follows the device. The symbol at the top turns the colours round until you log out.",
    "Hell": "Light",
    "Dunkel": "Dark",
    "System": "System",
    "{label}: unbekannte Auswahl.": "{label}: unknown choice.",
    "Version {version}": "Version {version}",
    "Alpha": "Alpha",
    "Beta": "Beta",
    "Release Candidate": "Release candidate",
    "Holt die Buchungen deiner Broker und Banken und schreibt sie ohne Duplikate in dein selbst gehostetes "
    "Wealthfolio – dazu Länder und Sektoren, ISINs, Logos und X-Ray.":
        "Fetches the transactions of your brokers and banks and writes them, without duplicates, into your "
        "self-hosted Wealthfolio - plus countries and sectors, ISINs, logos and X-Ray.",
    "Website": "Website",
    "Doku": "Docs",
    "Fragen oder einen Fehler gefunden?": "Have questions or found a bug?",
    "Fehler melden": "Report a bug",
    "Sicherheitslücke melden": "Report a vulnerability",
    "Lizenz: MIT": "Licence: MIT",
    "Danksagungen:": "Credits:",
    "Datenschutz:": "Privacy:",
    "Der Dienst sendet Daten nur an deine Broker und Banken (bei PSD2-Banken über Enable Banking), an dein "
    "Wealthfolio, an deinen ntfy-Server (wenn Benachrichtigungen eingerichtet sind), an die Seiten der "
    "ETF-Anbieter und an Finanzfluss (die ISINs deiner Fonds), an logo.dev (ISINs, nur wenn Logos an ist), an das "
    "ESMA-Register FIRDS (ISINs, nur wenn die Anlageklassen-Abfrage an ist) und "
    "an GitHub (Suche nach Updates, Download der Scalable CLI) – sonst nirgendwohin.":
        "The service sends data only to your brokers and banks (PSD2 banks through Enable Banking), your "
        "Wealthfolio, your ntfy server (when notifications are set up), the ETF issuers' sites and Finanzfluss "
        "(your funds' ISINs), logo.dev (ISINs, only while Logos is on), ESMA's FIRDS register (ISINs, only while "
        "the asset-class lookup is on) and GitHub (update check, download of the Scalable CLI) - nowhere else.",
    # ── common words ──
    "Speichern": "Save",
    "Gespeichert.": "Saved.",
    "Entfernen": "Remove",
    "Hinzufügen": "Add",
    "Keine.": "None.",
    "Datum": "Date",
    "Art": "Type",
    "Betrag": "Amount",
    "Wertpapier": "Security",
    "Text": "Text",
    "Name": "Name",
    "Status": "Status",
    "Status: {s}": "Status: {s}",
    "Konto": "Account",
    "Kommentar": "Comment",
    "Herkunft": "Source",
    "Dienst": "Service",
    "Import / von Hand": "Import / by hand",
    "Zeitpunkt": "Time",
    "Wirkung": "Effect",
    "Stück": "Shares",
    "Kurs": "Price",
    "Gebühr": "Fee",
    "Gebühren": "Fees",
    "Kaufdatum": "Purchase date",
    "Vorgang": "Transaction",
    "auswählen": "select",
    "… und {n} weitere.": "… and {n} more.",
    "Verrechnungskonto": "Cash account",
    "Depotkonto": "Securities account",
    "Unbekannter Broker.": "Unknown broker.",
    "Wealthfolio nicht erreichbar: {error}": "Wealthfolio not reachable: {error}",
    "Formular abgelaufen - bitte die Seite neu laden.": "Form expired - please reload the page.",
    # ── overview ──
    "alles in Ordnung": "all fine",
    "bitte ansehen": "please have a look",
    "Handlungsbedarf": "Action needed",
    "noch kein Abruf": "not fetched yet",
    "{label} entfernt": "{label} removed",
    "Den Test-Broker „{label}“ gibt es nicht mehr. Seine {n} Buchungen in Wealthfolio bleiben, bis du sie löschst – "
    "Suchbegriff <code>{search}</code> im Kommentar.":
        "The test broker “{label}” no longer exists. Its {n} bookings stay in Wealthfolio until you delete them – "
        "search for <code>{search}</code> in the comment.",
    "Ausblenden": "Hide",
    "Adresse der Seite nach der Anmeldung": "Address of the page after signing in",
    "Update verfügbar: Version {new}": "Update available: version {new}",
    "Installiert ist {current}. Zum Aktualisieren in der Konsole des Containers <code>update</code> eingeben.":
        "Installed is {current}. To update, type <code>update</code> in the container's console.",
    "Was ist neu?": "What's new?",
    "Abruf läuft …": "Fetching …",
    "Alle abrufen": "Fetch all",
    "Sicherer Modus: Jeder Abruf beginnt mit deiner Anmeldung, danach meldet sich der Dienst ab.":
        "Safe mode: every fetch starts with your login, afterwards the service logs out.",
    "Ändern": "Change",
    "Automatischer Abruf:": "Automatic fetch:",
    "nächster Lauf {when}": "next run {when}",
    "täglich": "daily",
    "Broker ohne automatischen Abruf nur einzeln": "brokers without automatic fetch only one by one",
    "Wealthfolio nicht verbunden": "Wealthfolio not connected",
    "Wealthfolio verbinden": "Connect Wealthfolio",
    "nur manuell": "manual only",
    "Zuletzt {when} · <strong>{n}</strong> neu": "Last {when} · <strong>{n}</strong> new",
    "{n} vorhanden": "{n} already there",
    "Noch nie abgerufen": "Never fetched",
    'Saldo <strong class="ok-text">stimmt</strong>': 'Balance <strong class="ok-text">matches</strong>',
    'Saldo <strong class="warn-text">weicht ab</strong>': 'Balance <strong class="warn-text">differs</strong>',
    "Meldung des letzten Abrufs": "Message of the last fetch",
    "Abweichungen zu Wealthfolio ({n})": "Differences from Wealthfolio ({n})",
    "Wert": "Value",
    'Stand {when}. Gemeldet wird erst, was beim nächsten Abruf noch abweicht. Bestände von vor dem Startdatum unter '
    '<a href="/check">Prüfung</a> anlegen.':
        'As of {when}. Only what still differs at the next fetch is reported. Enter positions from before the start '
        'date under <a href="/check">Check</a>.',
    "Jetzt abrufen": "Fetch now",
    "Noch kein Broker": "No broker yet",
    "Broker einrichten": "Set up a broker",
    "Broker hinzufügen": "Add broker",
    "Letzte Läufe": "Last runs",
    "{n} neu": "{n} new",
    "{n} Fehler": "{n} errors",
    "Ältere Läufe ({n})": "Older runs ({n})",
    "Letzter Abruf fehlgeschlagen": "Last fetch failed",
    "Letzter Abruf fehlgeschlagen ({n} Buchungen)": "Last fetch failed ({n} bookings)",
    "{n} Abweichung zu Wealthfolio": "{n} difference from Wealthfolio",
    "{n} Abweichungen zu Wealthfolio": "{n} differences from Wealthfolio",
    "{n} unbekannte Buchung": "{n} unknown booking",
    "{n} unbekannte Buchungen": "{n} unknown bookings",
    "{n} fehlt in Wealthfolio": "{n} missing in Wealthfolio",
    "Letzter Abruf abgebrochen": "Last fetch aborted",
    "Seit {n} Tagen kein Abruf": "No fetch for {n} days",
    "Ein Abruf läuft bereits.": "A fetch is already running.",
    "Abruf gestartet. Die Seite aktualisiert sich, bis er fertig ist.":
        "Fetch started. The page refreshes until it is done.",
    "Abruf beendet.": "Fetch finished.",
    # ── check ──
    "Broker und Wealthfolio im Vergleich. Der Dienst legt nichts von selbst wieder an und löscht nichts – du "
    "entscheidest.":
        "Broker and Wealthfolio compared. The service neither re-creates nor deletes anything on its own – you decide.",
    "{n} offene Punkte": "{n} open points",
    "Fehlt in Wealthfolio <strong>{n}</strong>": "Missing in Wealthfolio <strong>{n}</strong>",
    "Nicht mehr bei {label} <strong>{n}</strong>": "No longer at {label} <strong>{n}</strong>",
    "Bestand ohne Kauf <strong>{n}</strong>": "Position without purchase <strong>{n}</strong>",
    "Noch kein Abruf": "Not fetched yet",
    "Bargeld im Depot <strong>{amount} EUR</strong>": "Cash in the securities account <strong>{amount} EUR</strong>",
    "Überträge ohne Gegenstück <strong>{n}</strong>": "Transfers without counterpart <strong>{n}</strong>",
    "Fehlt in Wealthfolio: {n}": "Missing in Wealthfolio: {n}",
    "Schon übernommen, in Wealthfolio aber gelöscht. <em>Wieder anlegen</em> bucht sie beim nächsten Abruf neu.":
        "Synced before, but deleted in Wealthfolio. <em>Book again</em> books them at the next fetch.",
    "Fehlt": "Missing",
    "ganz": "entirely",
    "teilweise": "partly",
    "Wieder anlegen": "Book again",
    "Ignorieren": "Ignore",
    "Nicht mehr bei {label}: {n}": "No longer at {label}: {n}",
    "Vom Broker storniert oder entfernt – in Wealthfolio prüfen und löschen (bei Überträgen beide Seiten).":
        "Cancelled or removed by the broker – check and delete them in Wealthfolio (both sides of a transfer).",
    "Bestand ohne Kauf: {n}": "Position without purchase: {n}",
    "Mehr Stück als Wealthfolio kennt (vor dem Startdatum gekauft oder übertragen). Der nächste Abruf bucht einen Kauf "
    "samt Einzahlung.":
        "More shares than Wealthfolio knows (bought or transferred before the start date). The next fetch books a "
        "purchase with its deposit.",
    "Kurs pro Stück ({currency})": "Price per share ({currency})",
    "Als Kauf anlegen": "Book as purchase",
    "wird beim nächsten Abruf gebucht": "booked at the next fetch",
    "Bargeld bleibt im Depot: {n} Zeitpunkte, {amount} EUR":
        "Cash left in the securities account: {n} moments, {amount} EUR",
    "Jedem Block fehlt eine Seite – z. B. ein Kauf ohne Übertrag vom {account}, oder eine Buchung doppelt. Korrigieren "
    "in Wealthfolio.":
        "Each block lacks a side – e.g. a purchase without a transfer from {account}, or a booking twice. Correct it "
        "in Wealthfolio.",
    "Buchungen zeigen": "Show bookings",
    "bleibt stehen: {amount} EUR": "left: {amount} EUR",
    "Überträge ohne Gegenstück: {n}": "Transfers without counterpart: {n}",
    "Auf dem anderen Konto fehlt der passende Übertrag.": "The matching transfer is missing on the other account.",
    "Ignoriert ({n})": "Ignored ({n})",
    "Doch anlegen": "Book after all",
    "Noch kein Broker mit zwei Wealthfolio-Konten eingerichtet.": "No broker with two Wealthfolio accounts set up yet.",
    "Was wird geprüft?": "What is checked?",
    "<strong>Fehlt in Wealthfolio:</strong> Vom Dienst gebucht, in Wealthfolio aber nicht mehr da – oder die "
    "Import-Buchung, der er sie zugeordnet hatte, ist weg. <em>Ignorieren</em>, wenn sie zu Recht fehlt.":
        "<strong>Missing in Wealthfolio:</strong> booked by the service, but no longer in Wealthfolio – or the "
        "imported booking it was matched to is gone. <em>Ignore</em> it if it is missing on purpose.",
    "<strong>Nicht mehr beim Broker:</strong> nur für Broker, die jedes Mal die ganze Historie liefern.":
        "<strong>No longer at the broker:</strong> only for brokers that deliver their whole history every time.",
    "<strong>Bargeld im Depot:</strong> Das Depotkonto hält kein Bargeld – jeder Kauf kommt vom Verrechnungskonto, "
    "jeder Verkauf und jede Ausschüttung geht dorthin. Buchungen wenige Sekunden auseinander bilden einen Block; zwei "
    "Blöcke, die sich binnen 36 Stunden ausgleichen, gelten als zusammengehörig.":
        "<strong>Cash in the securities account:</strong> the securities account holds no cash – every purchase "
        "comes from the cash account, every sale and payout goes there. Bookings a few seconds apart form a block; "
        "two blocks that cancel out within 36 hours count as a pair.",
    "<strong>Überträge ohne Gegenstück:</strong> gleicher Betrag, gleiche Gruppe oder höchstens eine Minute "
    "auseinander.":
        "<strong>Transfers without counterpart:</strong> same amount, same group or at most one minute apart.",
    "Anfangsbestand entfernt.": "Opening position removed.",
    "Bitte Kaufdatum, Stückzahl, Kurs und Gebühr als Zahlen angeben.":
        "Please give purchase date, shares, price and fee as numbers.",
    "Stückzahl und Kurs müssen größer 0 sein, die Gebühr mindestens 0, das Kaufdatum darf nicht in der Zukunft "
    "liegen.":
        "Shares and price must be greater than 0, the fee at least 0, and the purchase date must not be in the future.",
    "Anfangsbestand {isin} gespeichert - der nächste Abruf bucht ihn als Kauf am {day} samt Einzahlung des "
    "Kaufbetrags.":
        "Opening position {isin} saved - the next fetch books it as a purchase on {day}, with a deposit of the "
        "purchase amount.",
    "Keine Buchung ausgewählt.": "No booking selected.",
    "{n} Buchung(en) werden nicht mehr gebucht.": "{n} booking(s) will no longer be booked.",
    "Unbekannte Aktion.": "Unknown action.",
    "{n} Buchung(en) legt der nächste Abruf wieder an – außer sie sind inzwischen in Wealthfolio (z. B. aus einem "
    "Import).":
        "The next fetch books {n} booking(s) again – unless they are in Wealthfolio by then (e.g. from an import).",
    # ── unknown ──
    "Unbekannte Buchungen": "Unknown bookings",
    "Diese Buchungen kennt der Dienst noch nicht. Sie wurden <strong>nicht</strong> nach Wealthfolio übernommen – "
    "vervollständige sie hier, dann bucht der nächste Abruf sie, oder trage sie von Hand ein. Mit <em>Fehlerbericht "
    "auf GitHub</em> meldest du einen Typ, damit er künftig "
    "unterstützt wird: GitHub öffnet sich mit vorausgefülltem Text (IBANs und lange Nummern geschwärzt). Der Bericht "
    "ist öffentlich – prüfe ihn vor dem Absenden.":
        "The service doesn't know these bookings yet. They were <strong>not</strong> transferred to Wealthfolio – "
        "complete them here and the next fetch books them, or enter them by hand. With <em>Report on GitHub</em> "
        "you report a type so it is supported in "
        "future: GitHub opens with a pre-filled text (IBANs and long numbers blacked out, in German). The report is "
        "public – check it before you send it.",
    "Kauf": "Buy",
    "Verkauf": "Sale",
    "Einbuchung ohne Zahlung": "Delivered in without payment",
    "Ausbuchung ohne Zahlung": "Delivered out without payment",
    "Split / Reverse Split": "Split / reverse split",
    "Vervollständigen und buchen": "Complete and book",
    "Vorgemerkt als {what} ({isin}) – der nächste Abruf bucht es.":
        "Noted as {what} ({isin}) – the next fetch books it.",
    "Zurücknehmen": "Withdraw",
    "Was ist es?": "What is it?",
    "Betrag (EUR)": "Amount (EUR)",
    "Steuer": "Tax",
    "Verhältnis (neue Stück je alte, 1:10 = 0,1)": "Ratio (new shares per old one, 1:10 = 0.1)",
    "Kauf und Verkauf: Stück und Betrag (was vom bzw. aufs Konto ging). Einbuchung: Stück und Einstandswert. "
    "Ausbuchung: nur Stück. Split: nur das Verhältnis. Datum ist das der Buchung.":
        "Buy and sale: shares and amount (what left or reached the account). Delivered in: shares and cost basis. "
        "Delivered out: shares only. Split: the ratio only. The date is the booking's.",
    "Diese Buchung ist nicht mehr offen.": "This booking is no longer open.",
    "Zurückgenommen.": "Withdrawn.",
    "Bitte wählen, was die Buchung ist.": "Please choose what the booking is.",
    "Verhältnis": "Ratio",
    "{label}: bitte angeben.": "{label}: please enter it.",
    "Stückzahl, Betrag und Verhältnis müssen größer 0 sein.": "Shares, amount and ratio must be greater than 0.",
    "Gespeichert - der nächste Abruf bucht es.": "Saved – the next fetch books it.",
    "Wertpapier-Buchung ohne Gegenstück – die Wertpapierabrechnung (PDF) in Wealthfolio importieren, dann erkennt der "
    "nächste Abruf sie":
        "Securities booking without counterpart – import the securities statement (PDF) into Wealthfolio, then the "
        "next fetch recognises it",
    "({n} Stück)": "({n} shares)",
    "Rohdaten": "Raw data",
    "Fehlerbericht auf GitHub": "Report on GitHub",
    # ── brokers ──
    "aktiv": "active",
    "aus": "off",
    "Einrichten": "Set up",
    "Zugang": "Access",
    "•••••••• (gespeichert – leer lassen zum Behalten)": "•••••••• (saved – leave empty to keep it)",
    "•••••••• (gespeichert)": "•••••••• (saved)",
    "{label} nicht mehr speichern – bei jeder Anmeldung eingeben":
        "Don't store {label} any more – type it at every login",
    "Wealthfolio-Konten": "Wealthfolio accounts",
    "Verbindung prüfen": "Check the connection",
    "Ein Verrechnungskonto für Ein- und Auszahlungen, ein Depotkonto für die Wertpapiere. Hast du schon per CSV/PDF "
    "importiert, nimm dieselben Konten, dann werden diese Buchungen erkannt.":
        "A cash account for deposits and withdrawals, a securities account for the securities. If you already "
        "imported CSV/PDF files, choose the same accounts so those bookings are recognised.",
    "Verrechnungskonto (Cash)": "Cash account",
    "– wählen –": "– choose –",
    "{label}-Konto (optional)": "{label} account (optional)",
    "– nicht abrufen –": "– don't fetch –",
    "Ein eigenes Wealthfolio-Konto für das {label}: Der Dienst liest dann auch dessen Umsätze und Saldo. Umbuchungen "
    "zwischen Girokonto und {label} werden als ein Übertrag gebucht. Beim ersten Abruf holt er das {label} ab dem "
    "Startdatum.":
        "A Wealthfolio account of its own for the {label}: the service then also reads its transactions and balance. "
        "Transfers between current account and {label} are booked as one transfer. The first fetch reads the "
        "{label} from the start date.",
    "Buchungen übernehmen ab (beim ersten Abruf)": "Take over bookings from (at the first fetch)",
    "Leer = alles, was der Broker liefert. Gilt für den ersten Abruf; danach holt jeder Abruf ab dem letzten (minus 7 "
    "Tage) – für einen neuen Zeitraum unten „Ab Startdatum neu abrufen“.":
        "Empty = everything the broker delivers. Applies to the first fetch; after that every fetch reads from the "
        "last one (minus 7 days) – for a new period use “Fetch again from the start date” below.",
    "Automatisch abrufen": "Fetch automatically",
    "Beim Broker anmelden": "Log in at the broker",
    "Fehlen Buchungen aus der Zeit vor dem letzten Abruf (etwa nach einem geänderten Startdatum)? Der nächste Abruf "
    "holt dann noch einmal alles ab dem Startdatum; schon übernommene Buchungen werden nicht doppelt angelegt.":
        "Bookings from before the last fetch missing (e.g. after changing the start date)? The next fetch then reads "
        "everything from the start date again; bookings synced before are not booked twice.",
    "Ab Startdatum neu abrufen": "Fetch again from the start date",
    "Depotumsätze liefert die Schnittstelle nicht. Exportiere sie im Online-Banking (Depot → Umsätze → Export als CSV) "
    "und lade die Datei hier hoch – gern auch wiederholt oder überlappend, nichts wird doppelt gebucht. Der Dienst "
    "bucht Dividenden, Käufe, Verkäufe und Einbuchungen (z. B. Mitarbeiteraktien); Kapitalmaßnahmen und "
    "Depotüberträge stehen danach unter „Unbekannte Buchungen“. Konto- und Depotnummern aus der Datei werden nicht "
    "gespeichert.":
        "The interface doesn't deliver securities transactions. Export them in online banking (Depot → Umsätze → "
        "Export als CSV) and upload the file here – repeated or overlapping uploads are fine, nothing is booked "
        "twice. The service books dividends, purchases, sales and incoming deliveries (e.g. employee shares); "
        "corporate actions and securities transfers end up under “Unknown bookings”. Account and depot numbers "
        "from the file are not stored.",
    "Bisher hochgeladen: {n} Zeilen.": "Uploaded so far: {n} rows.",
    "Hochladen": "Upload",
    "Neu aufsetzen": "Start over",
    "Löscht alle Buchungen, die der Dienst für {label} in Wealthfolio angelegt hat (Kommentar „[SYNC {key}:…]“), und "
    "vergisst, was schon übernommen wurde. Der nächste Abruf holt dann alles ab dem Startdatum neu – nach den "
    "aktuellen Regeln. CSV-/PDF-Importe, von Hand angelegte Buchungen und eingetragene Anfangsbestände bleiben.":
        "Deletes every booking the service created in Wealthfolio for {label} (comment “[SYNC {key}:…]”) and forgets "
        "what was synced. The next fetch then reads everything from the start date again – under the current rules. "
        "CSV/PDF imports, bookings entered by hand and opening positions stay.",
    "Ja, die Buchungen des Dienstes für {label} löschen": "Yes, delete the service's bookings for {label}",
    "Bitte ein Verrechnungs- und ein Depotkonto wählen.": "Please choose a cash account and a securities account.",
    "Verrechnungs- und Depotkonto müssen verschiedene Konten sein.":
        "The cash account and the securities account must be different accounts.",
    "Das {label}-Konto muss ein eigenes Wealthfolio-Konto sein, nicht das Verrechnungs- oder Depotkonto.":
        "The {label} account must be a Wealthfolio account of its own, not the cash or securities account.",
    "Gespeichert. Jetzt beim Broker anmelden.": "Saved. Now log in at the broker.",
    "Der nächste Abruf holt noch einmal alles ab {start}. Schon übernommene Buchungen werden nicht doppelt angelegt.":
        "The next fetch reads everything from {start} again. Bookings synced before are not booked twice.",
    "Der nächste Abruf holt noch einmal alles, was der Broker liefert. Schon übernommene Buchungen werden nicht "
    "doppelt angelegt.":
        "The next fetch reads everything the broker delivers again. Bookings synced before are not booked twice.",
    "Für diesen Broker gibt es keinen Datei-Import.": "This broker has no file import.",
    "Bitte eine Datei auswählen.": "Please choose a file.",
    "Die Datei ist zu groß (höchstens 5 MB).": "The file is too large (at most 5 MB).",
    "Dividenden": "dividends",
    "Käufe/Einbuchungen": "purchases/incoming deliveries",
    "Verkäufe": "sales",
    "nicht automatisch buchbar": "not bookable automatically",
    "{rows} Zeilen gelesen ({summary}), {new} davon neu. Der nächste Abruf bucht sie; nicht Buchbares steht dann unter "
    "„Unbekannte Buchungen“.":
        "{rows} rows read ({summary}), {new} of them new. The next fetch books them; what can't be booked ends up "
        "under “Unknown bookings”.",
    "Bitte bestätige das Neu-Aufsetzen mit dem Häkchen.": "Please confirm starting over with the tick.",
    "Erst die beiden Wealthfolio-Konten wählen.": "Choose the two Wealthfolio accounts first.",
    "Gerade läuft ein Abruf - bitte gleich noch einmal.": "A fetch is running - please try again in a moment.",
    "{deleted} Buchungen gelöscht, {failed} nicht: {errors} - bitte noch einmal versuchen.":
        "{deleted} bookings deleted, {failed} not: {errors} - please try again.",
    "{deleted} Buchungen des Dienstes gelöscht. Der nächste Abruf holt alles ab dem Startdatum neu.":
        "{deleted} bookings of the service deleted. The next fetch reads everything from the start date again.",
    "Startdatum": "Start date",
    # ── broker login ──
    "Anmeldung {label}": "Login {label}",
    "Anmeldung {n} von {total}": "Login {n} of {total}",
    "Anmeldung bei {label}": "Login at {label}",
    "TAN-Grafik": "TAN image",
    "Link öffnen": "Open link",
    "Code": "Code",
    "Bestätigen": "Confirm",
    "Anmelden": "Log in",
    "Ich habe bestätigt": "I have confirmed",
    "Ich habe in der App bestätigt": "I have confirmed in the app",
    "Nach der letzten Anmeldung werden alle zusammen abgerufen.": "After the last login all are fetched together.",
    "Danach ruft der Dienst gleich ab.": "The service fetches right after.",
    "Sicherer Modus: Nach dem Abruf meldet er sich wieder ab.": "Safe mode: after the fetch it logs out again.",
    "{label} überspringen": "Skip {label}",
    "{label} übersprungen.": "{label} skipped.",
    "Die Anmeldung ist abgelaufen, bitte neu starten.": "The login has expired, please start again.",
    "{label}: angemeldet. Du kannst jetzt abrufen.": "{label}: logged in. You can fetch now.",
    "{label}: angemeldet - die gespeicherte Sitzung ist noch gültig, eine Bestätigung war nicht nötig. Du kannst "
    "jetzt abrufen.":
        "{label}: logged in - the saved session is still valid, no confirmation was needed. You can fetch now.",
    "{label}: Anmeldung fehlgeschlagen: {error}": "{label}: login failed: {error}",
    "Anmeldung fehlgeschlagen: {error}": "Login failed: {error}",
    "Nichts abzurufen.": "Nothing to fetch.",
    "Übersprungen.": "Skipped.",
    # ── duplicates, transfers ──
    "Doppelte Buchungen": "Duplicate bookings",
    "Hier stehen Buchungen, die der Dienst angelegt hat, obwohl es sie schon aus einem CSV- oder PDF-Import gab. Bis "
    "Version 0.3.1 erkannte der Dienst Wertpapiere nur am Symbol; ein Import führt sie aber oft unter einem "
    "zugeordneten Ticker, der Dienst unter der ISIN.":
        "These are bookings the service created although they already existed from a CSV or PDF import. Up to "
        "version 0.3.1 the service recognised securities by their symbol only; an import often keeps them under a "
        "mapped ticker, the service under the ISIN.",
    "Gelöscht wird nur die Kopie des Dienstes (Kommentar „[SYNC …]“) samt ihrer Übertragsbuchungen. Deine "
    "CSV-/PDF-Buchung bleibt, und der Dienst legt den Vorgang nicht noch einmal an.":
        "Only the service's copy (comment “[SYNC …]”) and its transfer bookings are deleted. Your CSV/PDF booking "
        "stays, and the service doesn't book the transaction again.",
    "{label}: {n} doppelt": "{label}: {n} duplicates",
    "Vom Dienst": "By the service",
    "Schon vorhanden (CSV/PDF)": "Already there (CSV/PDF)",
    "Ich habe die Liste geprüft – die Kopien des Dienstes aus Wealthfolio löschen":
        "I have checked the list – delete the service's copies from Wealthfolio",
    "{n} Duplikate löschen": "Delete {n} duplicates",
    "Bitte bestätige das Löschen mit dem Häkchen.": "Please confirm the deletion with the tick.",
    "Wealthfolio: {error}": "Wealthfolio: {error}",
    "{n} doppelte Vorgänge entfernt ({deleted} Buchungen). Die CSV-/PDF-Buchungen bleiben.":
        "{n} duplicate transactions removed ({deleted} bookings). The CSV/PDF bookings stay.",
    " Der Abgleich wird beim nächsten Abruf neu berechnet - auf der Startseite bei {label} „Jetzt abrufen“ klicken.":
        " The comparison is recomputed at the next fetch - click “Fetch now” for {label} on the overview.",
    " {n} nicht entfernt: {errors}": " {n} not removed: {errors}",
    "Überträge auf eigene Konten": "Transfers to your own accounts",
    "Eine ausgehende Überweisung zählt in Wealthfolio sonst als Ausgabe. Geht sie auf ein eigenes Konto (z.&nbsp;B. "
    "dein Trade-Republic-Verrechnungskonto), trag es hier ein. Dann wird sie als Übertrag gebucht, mit dem Gegenstück "
    "auf dem gewählten Wealthfolio-Konto.":
        "An outgoing payment otherwise counts as spending in Wealthfolio. If it goes to an account of your own "
        "(e.g. your Trade Republic cash account), enter it here. It is then booked as a transfer, with its "
        "counterpart on the chosen Wealthfolio account.",
    "Eingehende Überweisungen bleiben immer Einzahlungen.": "Incoming payments always stay deposits.",
    "Stichwort": "Keyword",
    "Gegenkonto in Wealthfolio": "Counter account in Wealthfolio",
    "– nur als Übertrag markieren –": "– only mark as transfer –",
    "IBAN des Empfängers": "IBAN of the recipient",
    "oder Stichwort im Verwendungszweck": "or keyword in the payment reference",
    "Zielkonto": "Target account",
    "Bitte einen Namen und eine IBAN oder ein Stichwort angeben.": "Please give a name and an IBAN or a keyword.",
    # ── settings pages ──
    "Wealthfolio-Adresse": "Wealthfolio address",
    "Die Adresse, unter der du Wealthfolio im Browser öffnest, und das Wealthfolio-Passwort.":
        "The address you open Wealthfolio at in the browser, and the Wealthfolio password.",
    "Adresse": "Address",
    "Passwort": "Password",
    "Wealthfolio läuft ohne Passwort": "Wealthfolio runs without a password",
    "Speichern und Verbindung testen": "Save and test the connection",
    "Das Passwort steht nach der Installation von Wealthfolio im Wealthfolio-Container in "
    "<code>~/wealthfolio.creds</code>.":
        "After installing Wealthfolio, the password is in <code>~/wealthfolio.creds</code> in the Wealthfolio "
        "container.",
    "Verbindung fehlgeschlagen: {error}": "Connection failed: {error}",
    "Verbunden - {n} Konten in Wealthfolio gefunden.": "Connected - {n} accounts found in Wealthfolio.",
    'Der Dienst meldet sich per <a href="https://ntfy.sh" target="_blank" rel="noopener">ntfy</a>, wenn eine TAN '
    "fällig ist, eine Anmeldung abgelaufen ist oder ein Abruf fehlschlägt. Installiere die ntfy-App auf dem Handy und "
    "abonniere dort das Thema unten.":
        'The service notifies you via <a href="https://ntfy.sh" target="_blank" rel="noopener">ntfy</a> when a TAN '
        "is due, a login has expired or a fetch fails. Install the ntfy app on your phone and subscribe to the topic "
        "below there.",
    "ntfy-Server": "ntfy server",
    "Thema": "Topic",
    "Auf ntfy.sh kann jeder ein Thema lesen, der den Namen kennt – der vorgeschlagene Name ist deshalb zufällig. "
    "Leer = keine Benachrichtigungen.":
        "On ntfy.sh anyone who knows a topic's name can read it – that's why the suggested name is random. "
        "Empty = no notifications.",
    "Zugangstoken": "Access token",
    "Zugangstoken (nur bei eigenem ntfy-Server mit Anmeldung)":
        "Access token (only for your own ntfy server with login)",
    "Adresse dieser Oberfläche": "Address of this interface",
    "Adresse dieser Oberfläche (für Links in den Nachrichten)": "Address of this interface (for links in the messages)",
    "Details in den Nachrichten (Beträge, Bestände, Fehlertexte)":
        "Details in the messages (amounts, positions, error texts)",
    "Aus: Die Nachricht sagt nur, was passiert ist, und verlinkt auf die Oberfläche. Auf einem öffentlichen "
    "ntfy-Server empfohlen.":
        "Off: the message only says what happened and links to the interface. Recommended on a public ntfy server.",
    "Speichern und Testnachricht senden": "Save and send a test message",
    "Thema: nur Buchstaben, Ziffern, - und _.": "Topic: only letters, digits, - and _.",
    "Testnachricht konnte nicht gesendet werden.": "The test message could not be sent.",
    "Testnachricht gesendet.": "Test message sent.",
    "Benachrichtigungen gespeichert.": "Notifications saved.",
    "Broker melden Wertpapiere per ISIN. Soll Wealthfolio ein Wertpapier unter einem anderen Symbol führen "
    "(z.&nbsp;B. dem Ticker, unter dem es schon in Wealthfolio steht), trage es hier ein. Ohne Eintrag wird die ISIN "
    "verwendet.":
        "Brokers report securities by ISIN. If Wealthfolio should keep a security under another symbol (e.g. the "
        "ticker it already has in Wealthfolio), enter it here. Without an entry the ISIN is used.",
    "{isin} entfernen": "Remove {isin}",
    "Symbol in Wealthfolio": "Symbol in Wealthfolio",
    "Börse (MIC, optional)": "Exchange (MIC, optional)",
    'Wealthfolio zeigt für Wertpapiere, die unter ihrer ISIN geführt werden, meist kein Logo. Eingeschaltet holt der '
    'Dienst nach jedem Abruf die Logos bei <a href="https://www.logo.dev" target="_blank" rel="noopener">logo.dev</a> '
    "und setzt sie in Wealthfolio als eigenes Logo des Wertpapiers (ab Wealthfolio 3.8.0).":
        "Wealthfolio mostly shows no logo for securities kept under their ISIN. Switched on, the service fetches the "
        'logos from <a href="https://www.logo.dev" target="_blank" rel="noopener">logo.dev</a> after every fetch and '
        "sets them in Wealthfolio as the security's custom logo (Wealthfolio 3.8.0 or later).",
    "Nur die ISINs deiner Wertpapiere gehen an logo.dev – keine Bestände, Beträge oder Konten.":
        "Only the ISINs of your securities go to logo.dev – no positions, amounts or accounts.",
    "Ein Logo, das du in Wealthfolio selbst gesetzt hast, bleibt unangetastet.":
        "A logo you set in Wealthfolio yourself is left alone.",
    "Jedes Logo wird einmal gesetzt. Entfernst du es in Wealthfolio, setzt der Dienst es nicht wieder.":
        "Every logo is set once. If you remove it in Wealthfolio, the service doesn't set it again.",
    "Hat logo.dev für eine ISIN kein Logo, fragt der Dienst erst nach 30 Tagen wieder.":
        "If logo.dev has no logo for an ISIN, the service only asks again after 30 days.",
    "logo.dev-Schlüssel (Publishable Key, <code>pk_…</code>)": "logo.dev key (publishable key, <code>pk_…</code>)",
    "Kostenlos nach Anmeldung bei logo.dev. Der Schlüssel liegt verschlüsselt im Tresor des Dienstes.":
        "Free after signing up at logo.dev. The key is kept encrypted in the service's vault.",
    "Schlüssel entfernen": "Remove the key",
    "{when}: {set} gesetzt, {none} ohne Logo bei logo.dev, {failed} fehlgeschlagen":
        "{when}: {set} set, {none} without a logo at logo.dev, {failed} failed",
    ", {n} beim nächsten Abruf": ", {n} at the next fetch",
    'Logos von <a href="https://logo.dev" target="_blank" rel="noopener">Logo.dev</a>.':
        'Logos provided by <a href="https://logo.dev" target="_blank" rel="noopener">Logo.dev</a>.',
    "Der Schlüssel sieht nicht wie ein logo.dev-Schlüssel aus.": "The key doesn't look like a logo.dev key.",
    "Ein Abruf läuft gerade - bitte gleich noch einmal.": "A fetch is running - please try again in a moment.",
    "Logos konnten nicht geholt werden - siehe Protokoll.": "Logos could not be fetched - see the log.",
    "{set} Logos gesetzt, {none} ohne Logo bei logo.dev, {failed} fehlgeschlagen.":
        "{set} logos set, {none} without a logo at logo.dev, {failed} failed.",
    "Unbekannte Auswahl.": "Unknown choice.",
    # ── form checks (web/inputs.py) ──
    "{label}: höchstens {max_len} Zeichen.": "{label}: at most {max_len} characters.",
    "{label}: enthält Steuerzeichen.": "{label}: contains control characters.",
    "{label}: bitte eine Adresse mit http:// oder https:// angeben.":
        "{label}: please give an address with http:// or https://.",
    "{label}: ohne Zugangsdaten, ? oder # in der Adresse.": "{label}: no credentials, ? or # in the address.",
    "{label}: bitte eine Adresse mit https:// angeben.": "{label}: please enter an address starting with https://.",
    "{label}: ohne Zugangsdaten oder # in der Adresse.": "{label}: no credentials or # in the address.",
    "{label}: bitte 8 Ziffern angeben.": "{label}: please enter 8 digits.",
    "{label}: ungültiger Port.": "{label}: invalid port.",
    "{label}: bitte als Datum angeben (JJJJ-MM-TT).": "{label}: please give a date (YYYY-MM-DD).",
    "{label}: darf nicht in der Zukunft liegen.": "{label}: must not be in the future.",
    "ISIN": "ISIN",
    "Keine gültige ISIN (12 Zeichen, Prüfziffer).": "Not a valid ISIN (12 characters, check digit).",
    "Unbekanntes Wertpapier-Kennzeichen.": "Unknown security identifier.",
    "IBAN": "IBAN",
    "Keine gültige IBAN (Prüfsumme stimmt nicht).": "Not a valid IBAN (the checksum doesn't match).",
    "Börse": "Exchange",
    "Börse: ein MIC aus 4 Zeichen, z. B. XETR.": "Exchange: a MIC of 4 characters, e.g. XETR.",
    "Symbol": "Symbol",
    "Symbol: 1-32 Zeichen, nur Buchstaben, Ziffern und . ^ : _ -":
        "Symbol: 1-32 characters, only letters, digits and . ^ : _ -",
    # ── first visit, login, unlock ──
    "Willkommen": "Welcome",
    "Lege zuerst ein Passwort für diese Oberfläche fest. Damit schützt du deine Broker-Zugänge vor anderen im "
    "Heimnetz.":
        "First choose a password for this interface. It protects your broker access from others on your home "
        "network.",
    "Passwort (mindestens 12 Zeichen)": "Password (at least 12 characters)",
    "Passwort wiederholen": "Repeat the password",
    "Passwort speichern": "Save the password",
    "Passwort vergessen? In der Konsole des Containers <code>companion-reset-password</code> ausführen.":
        "Forgot the password? Run <code>companion-reset-password</code> in the container's console.",
    "Das Passwort braucht mindestens {n} Zeichen, beide Eingaben müssen gleich sein.":
        "The password needs at least {n} characters, and both entries must be the same.",
    "Gerade laufen zu viele Anmeldungen gleichzeitig. Bitte gleich noch einmal versuchen.":
        "Too many sign-ins are running at the same time. Please try again in a moment.",
    "Der Einrichtungscode stimmt nicht oder ist abgelaufen.": "The setup code is wrong or has expired.",
    "Einrichtungscode": "Setup code",
    "Er stand am Ende der Installation. Einen neuen zeigt die Konsole des Containers: "
    "<code>companion-cli setup-code</code>":
        "It was shown at the end of the installation. The container's console shows a new one: "
        "<code>companion-cli setup-code</code>",
    "Am besten ein Passwortmanager: ein langes, zufälliges Passwort, das du nirgends sonst benutzt.":
        "Best use a password manager: a long, random password you use nowhere else.",
    "{label}: bitte als Zahl angeben.": "{label}: please enter a number.",
    "Zurück": "Back",
    "Formular abgelaufen": "Form expired",
    "Nicht erlaubt": "Not allowed",
    "Seite nicht gefunden": "Page not found",
    "Anfrage zu groß": "Request too large",
    "Zu viele Anfragen": "Too many requests",
    "Unerwarteter Fehler (Fehler-ID {id}). Die Einzelheiten stehen im Log des Dienstes.":
        "Unexpected error (error id {id}). The details are in the service's log.",
    "Zur Übersicht": "To the overview",
    "Der Dienst meldet sich bei {label} an; je nach Broker bestätigst du das in der App oder gibst eine TAN ein.":
        "The service signs in at {label}; depending on the broker you confirm it in the app or enter a TAN.",
    "Anmeldung starten": "Start sign-in",
    "Gerade läuft ein Abruf - bitte danach anmelden.": "A fetch is running - please sign in afterwards.",
    "Details auf ntfy.sh: Dort liest jeder mit, der das Thema kennt - bitte ausdrücklich bestätigen.":
        "Details on ntfy.sh: anyone who knows the topic reads along there - please confirm explicitly.",
    "Gespeichertes Token entfernen": "Remove the saved token",
    "Mir ist klar: Auf ntfy.sh liest jeder die Details mit, der das Thema kennt":
        "I understand: on ntfy.sh anyone who knows the topic reads the details",
    "Echte Nachrichten verlinken nur auf diese Oberfläche, nie auf eine fremde Seite.":
        "Real messages only link to this UI, never to another site.",
    "Mir ist klar: Danach schützt nur noch der Schlüssel des Containers die Daten - "
    "ein Backup des Containers öffnet sie.":
        "I understand: afterwards only the container's key protects the data - a backup of the container opens it.",
    "Zum Entfernen bitte bestätigen, dass die Daten danach ohne Passphrase gespeichert sind.":
        "To remove it, please confirm that the data are then stored without a passphrase.",
    "Zu viele Fehlversuche. Bitte in {n} Minuten erneut versuchen.":
        "Too many failed attempts. Please try again in {n} minutes.",
    "Falsches Passwort.": "Wrong password.",
    "Entsperren": "Unlock",
    "Gesperrt": "Locked",
    "Alle Daten des Dienstes sind mit deiner Master-Passphrase verschlüsselt. Nach einem Neustart muss sie einmal "
    "eingegeben werden; bis dahin ruft der Dienst nichts ab.":
        "All of the service's data is encrypted with your master passphrase. After a restart it has to be entered "
        "once; until then the service fetches nothing.",
    # ── security ──
    "Master-Passphrase": "Master passphrase",
    "Gesetzt": "Set",
    "Nach jedem Neustart hier entsperren; bis dahin ruft der Dienst nichts ab und meldet sich per ntfy.":
        "Unlock here after every restart; until then the service fetches nothing and notifies you via ntfy.",
    "Schützt Zugangsdaten und Sync-Daten auch, wenn jemand den Container oder eine Sicherung kopiert. Nach jedem "
    "Neustart einmal entsperren. <strong>Ohne die Passphrase sind die Daten verloren</strong> – dann bleibt nur, den "
    "Dienst neu einzurichten.":
        "Protects credentials and sync data even if someone copies the container or a backup. Unlock once after "
        "every restart. <strong>Without the passphrase the data is lost</strong> – all that's left then is to set up "
        "the service again.",
    "Passwort der Oberfläche (zur Bestätigung)": "Password of this interface (to confirm)",
    "Neue Passphrase": "New passphrase",
    "Passphrase": "Passphrase",
    "(mindestens {n} Zeichen)": "(at least {n} characters)",
    "Wiederholen": "Repeat",
    "Passphrase ändern": "Change the passphrase",
    "Passphrase setzen": "Set the passphrase",
    "Passphrase entfernen": "Remove the passphrase",
    "Jetzt sperren": "Lock now",
    "Dem Zertifikat vertrauen": "Trust the certificate",
    "CA-Zertifikat herunterladen": "Download the CA certificate",
    "SHA-256 zum Vergleich:": "SHA-256 to compare:",
    "So importierst du es": "How to import it",
    "<strong>Windows (Chrome, Edge):</strong> Datei öffnen → <em>Zertifikat installieren</em> → <em>Aktueller "
    "Benutzer</em> → <em>Alle Zertifikate in folgendem Speicher speichern</em> → <em>Vertrauenswürdige "
    "Stammzertifizierungsstellen</em> – nicht „automatisch auswählen“, sonst landet es unter den "
    "Zwischenzertifizierungsstellen und wirkt nicht. Prüfen lässt es sich mit <code>certmgr.msc</code>.":
        "<strong>Windows (Chrome, Edge):</strong> open the file → <em>Install Certificate</em> → <em>Current "
        "User</em> → <em>Place all certificates in the following store</em> → <em>Trusted Root Certification "
        "Authorities</em> – not “automatically select”, or it ends up under the intermediate certification "
        "authorities and has no effect. Check it with <code>certmgr.msc</code>.",
    "<strong>macOS (Safari, Chrome):</strong> Datei öffnen (Schlüsselbundverwaltung) → Zertifikat doppelklicken → "
    "<em>Vertrauen</em> → <em>Immer vertrauen</em>.":
        "<strong>macOS (Safari, Chrome):</strong> open the file (Keychain Access) → double-click the certificate → "
        "<em>Trust</em> → <em>Always Trust</em>.",
    "<strong>Firefox:</strong> Einstellungen → <em>Datenschutz &amp; Sicherheit</em> → <em>Zertifikate anzeigen</em> "
    "→ <em>Zertifizierungsstellen</em> → <em>Importieren</em> → „Dieser CA vertrauen, um Websites zu identifizieren“.":
        "<strong>Firefox:</strong> Settings → <em>Privacy &amp; Security</em> → <em>View Certificates</em> → "
        "<em>Authorities</em> → <em>Import</em> → “Trust this CA to identify websites”.",
    "<strong>iPhone/iPad:</strong> Datei in Safari laden → <em>Einstellungen → Profil geladen → Installieren</em>, "
    "danach <em>Allgemein → Info → Zertifikatsvertrauenseinstellungen</em> einschalten.":
        "<strong>iPhone/iPad:</strong> download the file in Safari → <em>Settings → Profile Downloaded → "
        "Install</em>, then switch it on under <em>General → About → Certificate Trust Settings</em>.",
    "<strong>Android:</strong> <em>Einstellungen → Sicherheit → Verschlüsselung &amp; Anmeldedaten → Zertifikat "
    "installieren → CA-Zertifikat</em>.":
        "<strong>Android:</strong> <em>Settings → Security → Encryption &amp; credentials → Install a certificate → "
        "CA certificate</em>.",
    "Danach den Browser ganz beenden und neu starten (Chrome/Edge: <code>chrome://restart</code>; Android: App "
    "„Beenden erzwingen“) – ein Browser, der vorher die Warnung übergangen hat, zeigt die Seite sonst weiter als "
    "„nicht sicher“. Aufrufen über die IP-Adresse oder den Namen des Containers, wie im Zertifikat eingetragen.":
        "Then quit the browser completely and start it again (Chrome/Edge: <code>chrome://restart</code>; Android: "
        "“Force stop” the app) – a browser that skipped the warning before otherwise keeps showing the page as “not "
        "secure”. Open it via the IP address or the container's name, as entered in the certificate.",
    "Abrufmodus": "Fetch mode",
    "Automatisch": "Automatic",
    "Anmeldungen bei Brokern und Banken bleiben gespeichert, der Dienst ruft täglich von selbst ab.":
        "Logins at brokers and banks stay saved, the service fetches daily on its own.",
    "Sicher": "Safe",
    "Jeder Abruf beginnt mit deiner Anmeldung (App-Freigabe, TAN, Gerätecode), direkt danach meldet sich der Dienst "
    "wieder ab. Der tägliche Lauf ruft nicht ab, er erinnert dich per ntfy. Gilt für alle Broker.":
        "Every fetch starts with your login (app approval, TAN, device code), right after it the service logs out "
        "again. The daily run doesn't fetch, it reminds you via ntfy. Applies to all brokers.",
    "Prüfungen": "Checks",
    "Hinweis": "Note",
    "Verbindung zu Wealthfolio": "Connection to Wealthfolio",
    "Wealthfolio wird per HTTP angesprochen; das Wealthfolio-Passwort geht unverschlüsselt durchs Netz. Wenn möglich, "
    "Wealthfolio per HTTPS erreichbar machen.":
        "Wealthfolio is reached via HTTP; the Wealthfolio password crosses the network unencrypted. If possible, make "
        "Wealthfolio reachable via HTTPS.",
    "In Ordnung ({n})": "Fine ({n})",
    "Schlüssel des Dienstes": "Key of the service",
    "Außerhalb von data/, nur für root lesbar; der Dienst bekommt ihn beim Start (systemd-creds konnte hier nicht "
    "verschlüsseln).":
        "Outside data/, readable by root only; the service gets it at start (systemd-creds could not encrypt here).",
    "Aus COMPANION_KEY_FILE.": "From COMPANION_KEY_FILE.",
    "Liegt in data/ neben den verschlüsselten Daten - wer data/ kopiert, kann alles entschlüsseln. Ein Update richtet "
    "das ein.":
        "Kept in data/ next to the encrypted data - whoever copies data/ can decrypt everything. An update sets it "
        "up.",
    "Gesetzt: Ohne sie sind Zugangsdaten, Einstellungen und Sync-Daten nicht lesbar, auch nicht aus einer Sicherung.":
        "Set: without it credentials, settings and sync data can't be read, not even from a backup.",
    "Nicht gesetzt: Abrufe laufen nach einem Neustart ohne Zutun weiter. Wer den ganzen Container samt Schlüssel "
    "kopiert, kann die Daten entschlüsseln.":
        "Not set: fetches go on after a restart without you. Whoever copies the whole container with its key can "
        "decrypt the data.",
    "Zugangsdaten und Sitzungen": "Credentials and sessions",
    "AES-256-GCM (secrets.enc).": "AES-256-GCM (secrets.enc).",
    "Noch nichts gespeichert.": "Nothing stored yet.",
    "Altes Format - wird beim nächsten Lesen neu verschlüsselt.": "Old format - re-encrypted the next time it is read.",
    "AES-256-GCM (config.enc, Benachrichtigungen in notify.enc).":
        "AES-256-GCM (config.enc, notifications in notify.enc).",
    "config.json liegt noch unverschlüsselt vor.": "config.json is still there unencrypted.",
    "Sync-Datenbank": "Sync database",
    "Unverschlüsselt: SQLCipher gibt es für diese Plattform nicht (nur x86-64 und ARM64).":
        "Unencrypted: SQLCipher isn't available for this platform (x86-64 and ARM64 only).",
    "Mit SQLCipher verschlüsselt (AES-256).": "Encrypted with SQLCipher (AES-256).",
    "Noch unverschlüsselt - wird beim nächsten Öffnen verschlüsselt.":
        "Still unencrypted - encrypted the next time it is opened.",
    "Verbindung zur Oberfläche": "Connection to this interface",
    "Unverschlüsseltes HTTP - Passwörter gehen im Klartext durchs Netz.":
        "Unencrypted HTTP - passwords cross the network in plain text.",
    "HTTPS.": "HTTPS.",
    "HTTPS. Zertifikat bis {until}": "HTTPS. Certificate valid until {until}",
    "HTTPS. Zertifikat bis {until}, CA SHA-256 {ca}": "HTTPS. Certificate valid until {until}, CA SHA-256 {ca}",
    "Dateirechte": "File permissions",
    "Nur der Dienst-Benutzer kann data/ lesen.": "Only the service user can read data/.",
    "Für andere lesbar: {files}": "Readable for others: {files}",
    "Klartext-Reste": "Plain-text leftovers",
    "Unbekannter Abrufmodus.": "Unknown fetch mode.",
    "Sicherer Modus an: Alle gespeicherten Anmeldungen sind beendet. Jeder Abruf startet jetzt mit einer Anmeldung, "
    "danach meldet sich der Dienst wieder ab.":
        "Safe mode on: all saved logins have ended. Every fetch now starts with a login, afterwards the service logs "
        "out again.",
    "Automatischer Modus an: Anmeldungen bleiben gespeichert, der tägliche Abruf läuft ohne dich.":
        "Automatic mode on: logins stay saved, the daily fetch runs without you.",
    "Zu viele Fehlversuche. Bitte später erneut versuchen.": "Too many failed attempts. Please try again later.",
    "Das Passwort der Oberfläche stimmt nicht.": "The password of this interface is wrong.",
    "Die beiden Eingaben der Passphrase sind verschieden.": "The two passphrase entries differ.",
    "Die Passphrase braucht mindestens {n} Zeichen.": "The passphrase needs at least {n} characters.",
    "Gerade läuft ein Abruf. Bitte danach erneut versuchen.": "A fetch is running. Please try again afterwards.",
    "Master-Passphrase entfernt.": "Master passphrase removed.",
    "Master-Passphrase gesetzt. Merke sie dir gut: Ohne sie sind die Daten verloren.":
        "Master passphrase set. Remember it well: without it the data is lost.",
    # 0.21.0
    "Kontenprüfung":
        "Account check",
    "Passwort gespeichert. Die Seite Broker zeigt, was noch einzurichten ist.":
        "Password saved. The page Brokers shows what is left to set up.",
    "Dieses Passwort ist zu verbreitet und schnell erraten. Am besten eines aus einem Passwortmanager.":
        "This password is too common and quickly guessed. Best take one from a password manager.",
    "Passwort geändert. Alle anderen Anmeldungen sind beendet.":
        "Password changed. Every other sign-in has ended.",
    "Alle anderen Sitzungen sind beendet.":
        "All other sessions have ended.",
    "Das bisherige Passwort stimmt nicht.":
        "The current password is wrong.",
    "Diese Passphrase ist zu verbreitet und schnell erraten.":
        "This passphrase is too common and quickly guessed.",
    "{job} läuft im Hintergrund. Das Ergebnis erscheint hier, sobald es fertig ist.":
        "{job} runs in the background. The result shows here once it is done.",
    "Abruf":
        "Fetch",
    "Ersten Broker einrichten":
        "Set up the first broker",
    "Benachrichtigungen einrichten (für TAN-Anfragen und Fehler)":
        "Set up notifications (for TAN requests and errors)",
    "Optional: Master-Passphrase setzen":
        "Optional: set a master passphrase",
    "Optional: dem Zertifikat der Oberfläche vertrauen":
        "Optional: trust the certificate of this UI",
    "Gerade läuft noch: {job}. Bitte danach noch einmal.":
        "Still running: {job}. Please try again afterwards.",
    "{job} ist fehlgeschlagen - siehe Protokoll.":
        "{job} failed - see the log.",
    "Bitte genau „{label}“ eingeben.":
        "Please type exactly “{label}”.",
    "Die Konten werden gerade geprüft …":
        "The accounts are being checked …",
    "Konten geprüft {when}.":
        "Accounts checked {when}.",
    "Neu prüfen":
        "Check again",
    "Fehlt in Wealthfolio":
        "Missing in Wealthfolio",
    "Auswahl":
        "Selection",
    "Buchung vom {day} über {amount} {currency} auswählen":
        "Select the booking of {day} over {amount} {currency}",
    "Nicht mehr bei {label}":
        "No longer at {label}",
    "Anfangsbestände":
        "Opening positions",
    "Depot-Zeitpunkte mit Bargeld":
        "Securities account moments with cash",
    "Überträge ohne Gegenstück":
        "Transfers without a counterpart",
    "Ignorierte Buchungen":
        "Ignored bookings",
    "Anmeldung":
        "Login",
    "Buchen":
        "Booking",
    "Abgleich mit Wealthfolio":
        "Comparing with Wealthfolio",
    "{label} ({n} von {m})":
        "{label} ({n} of {m})",
    "{job} läuft …":
        "{job} running …",
    "Warte auf deine Freigabe in der App (seit {when})":
        "Waiting for your approval in the app (since {when})",
    "Die Seite aktualisiert sich alle 10 Sekunden.":
        "The page refreshes every 10 seconds.",
    "Automatisch aktualisieren anhalten":
        "Stop refreshing automatically",
    "Aktualisieren":
        "Refresh",
    "Einrichtung: {done} von {total} erledigt":
        "Setup: {done} of {total} done",
    "erledigt":
        "done",
    "optional":
        "optional",
    "offen":
        "open",
    "Abweichungen {label} zu Wealthfolio":
        "Deviations {label} vs. Wealthfolio",
    "Passwort der Oberfläche":
        "Password of this UI",
    "Bisheriges Passwort":
        "Current password",
    "Neues Passwort (mindestens {n} Zeichen)":
        "New password (at least {n} characters)",
    "Am besten eines aus einem Passwortmanager. Alle anderen Anmeldungen enden dabei.":
        "Best take one from a password manager. Every other sign-in ends with it.",
    "Passwort ändern":
        "Change password",
    "Angemeldet bleibt man höchstens 12 Stunden, nach 30 Minuten ohne Aktivität ist eine neue Anmeldung nötig.":
        "A sign-in lasts at most 12 hours; after 30 minutes without activity you sign in again.",
    "Alle anderen Sitzungen beenden":
        "End all other sessions",
    "{label} neu aufsetzen":
        "Start {label} over",
    "<strong>{n} Buchungen</strong> des Dienstes für {label} werden in Wealthfolio gelöscht (Kommentar "
    "„[SYNC {key}:…]“). Der nächste Abruf holt danach alles ab dem Startdatum neu. CSV-/PDF-Importe, von "
    "Hand angelegte Buchungen und eingetragene Anfangsbestände bleiben.":
        "<strong>{n} bookings</strong> of the service for {label} are deleted in Wealthfolio (comment “[SYNC "
        "{key}:…]”). The next fetch then gets everything from the start date again. CSV/PDF imports, bookings "
        "entered by hand and opening positions stay.",
    "Zum Bestätigen den Namen des Brokers eingeben: {label}":
        "To confirm, type the broker's name: {label}",
    "{n} Buchungen löschen":
        "Delete {n} bookings",
    "Abbrechen":
        "Cancel",
    "Passwort der Oberfläche (nur beim Ändern der Zugangsdaten)":
        "Password of this UI (only when changing the credentials)",
    "Neu aufsetzen …":
        "Start over …",
    "Zugangsdaten löschen":
        "Delete credentials",
    "Entfernt die gespeicherten Zugangsdaten und die Anmeldung bei {label} aus dem Tresor und schaltet den "
    "automatischen Abruf aus. Die Buchungen in Wealthfolio und die Einstellungen bleiben.":
        "Removes the stored credentials and the login at {label} from the vault and switches the automatic fetch "
        "off. The bookings in Wealthfolio and the settings stay.",
    "Zugangsdaten und Anmeldung gelöscht. Der automatische Abruf ist aus.":
        "Credentials and login deleted. The automatic fetch is off.",
    "Anfrage von einer anderen Website abgelehnt.":
        "Request from another website refused.",
    "Für eine neue Adresse bitte das Wealthfolio-Passwort noch einmal eingeben.":
        "For a new address, please enter the Wealthfolio password again.",
    "Übertragsmuster":
        "Transfer patterns",
    "Aktion":
        "Action",
    # 0.22.0
    "Mit systemd-creds und dem TPM verschlüsselt außerhalb von data/; eine Kopie des Containers allein "
    "öffnet ihn nicht.":
        "Encrypted with systemd-creds and the TPM outside data/; a copy of the container alone doesn't open "
        "it.",
    "Mit systemd-creds verschlüsselt außerhalb von data/, mit dem Host-Secret des Containers (kein TPM); "
    "der Dienst bekommt ihn nur beim Start.":
        "Encrypted with systemd-creds outside data/, with the container's host secret (no TPM); the service "
        "gets it only at start.",
    "Scalable CLI":
        "Scalable CLI",
    "Läuft in einer Sandbox (Landlock): kein Zugriff auf data/ und den Schlüssel.":
        "Runs in a sandbox (Landlock): no access to data/ and the key.",
    "Ohne Sandbox: Der Kernel kann kein Landlock. sc läuft als Dienst-Benutzer und könnte dessen Schlüssel und "
    "Daten lesen.":
        "Without a sandbox: the kernel has no Landlock. sc runs as the service user and could read its key and data.",
    "Nicht gesetzt, und ohne TPM: Ein Backup des Containers enthält Schlüssel und Daten. "
    "Master-Passphrase setzen oder die Backups verschlüsseln.":
        "Not set, and no TPM: a backup of the container holds key and data. Set a master passphrase or "
        "encrypt the backups.",
    "Abmeldung":
        "Sign-out",
    "Erstes Passwort festgelegt":
        "First password set",
    "Passwort geändert":
        "Password changed",
    "Alle anderen Sitzungen beendet":
        "All other sessions ended",
    "Zugangsdaten geändert":
        "Credentials changed",
    "Wealthfolio-Adresse gespeichert":
        "Wealthfolio address saved",
    "Master-Passphrase geändert":
        "Master passphrase changed",
    "Abrufmodus geändert":
        "Fetch mode changed",
    "Neu aufgesetzt":
        "Started over",
    "Entsperrt":
        "Unlocked",
    "Passwort für eine Aktion bestätigt":
        "Password confirmed for an action",
    "{label}: 64 Hex-Zeichen (SHA-256), mit oder ohne Doppelpunkte.":
        "{label}: 64 hex characters (SHA-256), with or without colons.",
    "Test erfolgreich - {n} Konten gefunden. Noch nichts gespeichert.":
        "Test passed - {n} accounts found. Nothing saved yet.",
    "Fingerprint":
        "Fingerprint",
    "Mit http:// gehen das Wealthfolio-Passwort und die Buchungen unverschlüsselt durchs Netz. Wenn "
    "Wealthfolio HTTPS kann – auch mit eigenem Zertifikat –, besser https:// nehmen.":
        "With http:// the Wealthfolio password and the bookings cross the network unencrypted. If Wealthfolio "
        "speaks HTTPS – with a certificate of its own too –, better use https://.",
    "Wealthfolio zeigt ein Zertifikat, dem kein bekannter Herausgeber vertraut. Sein Fingerabdruck "
    "(SHA-256): <code>{fp}</code>. Vergleiche ihn mit dem deines Wealthfolio (z. B. im Browser über das "
    "Schloss-Symbol); stimmt er, mit „Speichern“ vertrauen.":
        "Wealthfolio shows a certificate no known issuer vouches for. Its fingerprint (SHA-256): "
        "<code>{fp}</code>. Compare it with your Wealthfolio's (e.g. in the browser via the lock icon); if it "
        "matches, trust it with “Save”.",
    "Eigenes Zertifikat von Wealthfolio":
        "Wealthfolio's own certificate",
    "Fingerabdruck (SHA-256)":
        "Fingerprint (SHA-256)",
    "Nur für ein Wealthfolio mit selbst ausgestelltem Zertifikat: Der Dienst vertraut dann genau diesem "
    "Zertifikat, keinem anderen. Leer: die üblichen Zertifizierungsstellen.":
        "Only for a Wealthfolio with a certificate of its own: the service then trusts exactly this "
        "certificate, no other. Empty: the usual certificate authorities.",
    "Nur testen":
        "Test only",
    "Einmal auf dem Gerät importiert, verschwindet „Nicht sicher“ – auch nach jeder Erneuerung. Die CA "
    "kann nur diesen Container bestätigen: seinen Namen und seine Adressen, keinen Router und kein "
    "anderes Gerät. Ändert sich der Name oder eine Adresse, entsteht eine neue CA, die du einmal neu "
    "importierst.":
        "Imported once on a device, “Not secure” disappears – also after every renewal. The CA can only vouch "
        "for this container: its name and its addresses, no router and no other device. When the name or an "
        "address changes, a new CA is made, which you import once more.",
    "<strong>{fine} von {total}</strong> in Ordnung.":
        "<strong>{fine} of {total}</strong> fine.",
    "Zugangsdaten, Sitzungen und Einstellungen mit AES-256-GCM verschlüsselt.":
        "Credentials, sessions and settings encrypted with AES-256-GCM.",
    "Die Sync-Datenbank mit SQLCipher.":
        "The sync database with SQLCipher.",
    "Die Sync-Datenbank nicht: SQLCipher gibt es für diese Plattform nicht.":
        "The sync database not: there is no SQLCipher for this platform.",
    "Ereignisse ({n})":
        "Events ({n})",
    "Anmeldungen und Änderungen an Zugangsdaten, Passphrase und Abrufmodus der letzten 180 Tage, die "
    "neuesten zuerst. Ohne Passwörter und Zugangsdaten.":
        "Sign-ins and changes to credentials, passphrase and fetch mode of the last 180 days, newest first. "
        "Without passwords and credentials.",
    "Ereignisse":
        "Events",
    "Ereignis":
        "Event",
    "Von":
        "From",
    "Ergebnis":
        "Result",
    "abgelehnt":
        "refused",
    "Version {version} · Zugangsdaten und Einstellungen verschlüsselt, die Sync-Datenbank nicht · nur "
    "lesender Zugriff auf die Broker":
        "Version {version} · credentials and settings encrypted, the sync database not · read-only access to "
        "the brokers",
    # 0.23.0
    "Bitte bestätige, dass du den Hinweis zur Schnittstelle gelesen hast.":
        "Please confirm that you have read the note on the interface.",
    "Hinweis zur Schnittstelle bestätigt.":
        "Note on the interface confirmed.",
    "Ich habe den Hinweis gelesen und möchte den Zugang trotzdem nutzen.":
        "I have read the note and want to use the access anyway.",
    # Durchschau (waldonso2/wealthfolio-importer-addon#139)
    "Durchschau": "Look-through",
    "{name}: {n} Positionen gelesen, zusammen {sum} % des Fonds.":
        "{name}: {n} holdings read, {sum} % of the fund together.",
    "Unbekannte Position - bitte die Seite neu laden.": "Unknown position - please reload the page.",
    "Die Positionen werden gerade aus Wealthfolio gelesen …": "Reading the positions from Wealthfolio …",
    "Positionen gelesen {when}.": "Positions read {when}.",
    "Neu lesen": "Read again",
    "Was wird berechnet?": "What is calculated?",
    "Anteil am Depot: Wert der Position mal Gewicht der Aktie im Fonds, über alle Positionen addiert. Was die "
    "Gewichte eines Fonds offen lassen, steht als „nicht zugeordnet“ – nichts wird hochgerechnet. Überschneidung "
    "zweier ETFs: Summe des jeweils kleineren Gewichts jeder Aktie, die beide halten. Eigener Beitrag: der Teil "
    "eines ETFs, den keine andere Position mitbringt. Effektive Titelzahl: so vielen gleich gewichteten Aktien "
    "entspricht das Depot.":
        "Share of the portfolio: value of the position times the stock's weight in the fund, added up over all "
        "positions. What a fund's weights leave open is shown as \u201cunassigned\u201d - nothing is scaled up. "
        "Overlap of two ETFs: sum of the smaller weight of each stock both hold. Own contribution: the part of an "
        "ETF no other position brings. Effective number of stocks: as many equally weighted stocks as the portfolio "
        "is worth.",
    "Vereinfachte Modellrechnung, keine Anlageberatung.": "Simplified model calculation, not investment advice.",
    "Positionen und Positionslisten": "Positions and holdings lists",
    "Position": "Position",
    "Anteil am Depot": "Share of the portfolio",
    "Positionsliste": "Holdings list",
    "keine": "none",
    "Keine Positionen in Wealthfolio.": "No positions in Wealthfolio.",
    "Aktien über alle Positionen": "Stocks across all positions",
    "{n} Aktien, effektive Titelzahl {effective}.": "{n} stocks, effective number of stocks {effective}.",
    "Anzahl": "Number",
    "alle": "all",
    "Top {n}": "Top {n}",
    "Aktie": "Stock",
    "Land": "Country",
    "Sektor": "Sector",
    "davon über": "of which through",
    "Wie sich die ETFs überschneiden": "How the ETFs overlap",
    ("Überschneidung in Prozent: Summe des jeweils kleineren Gewichts jeder Aktie, die beide ETFs halten. "
     "Je dunkler, desto mehr doppelt."): (
        "Overlap in percent: the sum of the smaller weight of every stock both ETFs hold. "
        "The darker, the more is held twice."),
    "ETF": "ETF",
    "Eigener Beitrag": "Own contribution",
    # Klumpenrisiken und Abruf (#139)
    "Einzelaktie": "Single stock",
    "Fremdwährung": "Foreign currency",
    "Überschneidung": "Overlap",
    "Wenige Titel": "Few stocks",
    "Bargeld über der Einlagensicherung": "Cash above the deposit guarantee",
    "Einzelaktie über (%)": "Single stock above (%)",
    "Land über (%)": "Country above (%)",
    "Sektor über (%)": "Sector above (%)",
    "Fremdwährung über (%)": "Foreign currency above (%)",
    "Überschneidung zweier ETFs über (%)": "Overlap of two ETFs above (%)",
    "Effektive Titelzahl unter": "Effective number of stocks below",
    "Bargeld je Konto über (EUR)": "Cash per account above (EUR)",
    "{fetched} Positionslisten geholt, {none} ohne Quelle (hochladen), {failed} fehlgeschlagen.":
        "{fetched} holdings lists fetched, {none} without a source (upload), {failed} failed.",
    "Positionslisten abrufen": "Fetch holdings lists",
    "Meldung": "Finding",
    ("Das ist keine Produktseite von iShares (https://www.ishares.com/…/products/<Nummer>/<Name>) und keine "
     "Positionsliste von L&G (https://fundcentres.lgim.com/srp/documents-id/…)."): (
        "That is neither an iShares product page (https://www.ishares.com/…/products/<number>/<name>) nor an L&G "
        "holdings list (https://fundcentres.lgim.com/srp/documents-id/…)."),
    ("L&G: Öffne auf der Seite des ETFs den Download der Positionsliste (Fundholdings.csv), kopier seine Adresse und "
     "trag sie hier ein - einmal genügt, danach holt der Dienst die Liste jeden Tag selbst."): (
        "L&G: on the ETF's page, copy the address of the holdings list download (Fundholdings.csv) and enter it here "
        "- once is enough; after that the service fetches the list itself every day."),
    "Adresse der Positionsliste bei L&G für {name}": "Address of the L&G holdings list for {name}",
    "Erst den automatischen Abruf einschalten.": "Turn on automatic fetching first.",
    "Die Positionslisten konnten nicht geholt werden - siehe Protokoll.":
        "The holdings lists could not be fetched - see the log.",
    "Welche Aktien in deinen ETFs stecken und wie sie sich mit deinen anderen Positionen addieren. Die "
    "Positionslisten der ETFs holt der Dienst bei den Anbietern (unten einschalten) oder du lädst sie hoch; ohne "
    "Liste zählt ein ETF als eine Position.":
        "Which stocks your ETFs hold and how they add up with your other positions. The service fetches the ETFs' "
        "holdings lists from their issuers (turn it on below), or you upload them; without a list an ETF counts as "
        "one position.",
    "Klumpenrisiken: eine Aktie, ein Land, ein Sektor oder Fremdwährung über ihrer Grenze, zwei ETFs, die sich "
    "stark überschneiden, zu wenige effektive Titel, Bargeld über 100.000 EUR auf einem Konto (gesetzliche "
    "Einlagensicherung je Bank). Land, Sektor und Währung stehen so, wie der Anbieter sie nennt; ein "
    "währungsgesicherter ETF zählt noch als Fremdwährung. Bei synthetischen ETFs (Swap) listet der Anbieter oft den "
    "Sicherheitenkorb statt des Index.":
        "Cluster risks: a stock, a country, a sector or foreign currency above its limit, two ETFs that overlap a "
        "lot, too few effective stocks, cash above 100,000 EUR on one account (statutory deposit guarantee per "
        "bank). Country, sector and currency are as the issuer names them; a currency-hedged ETF still counts as "
        "foreign currency. For synthetic ETFs (swap) the issuer often lists the collateral basket instead of the "
        "index.",
    "Klumpenrisiken": "Cluster risks",
    "Was": "What",
    "Grenze": "Limit",
    "So gewollt": "Intended",
    "Keine Klumpenrisiken über deinen Grenzen.": "No cluster risks above your limits.",
    "So gewollt ({n})": "Intended ({n})",
    "Wieder melden": "Report again",
    "{n} Positionen von {source}, Stand {when}": "{n} holdings from {source}, as of {when}",
    "keine Quelle gefunden – Positionsliste hochladen, falls es ein ETF ist":
        "no source found – upload the holdings list if it is an ETF",
    "Quelle ändern oder Liste hochladen": "Change source or upload a list",
    "Produktseite bei iShares für {name}": "iShares product page for {name}",
    "Positionsliste für {name} (CSV, XLSX oder XLS)": "Holdings list for {name} (CSV, XLSX or XLS)",
    "Abruf fehlgeschlagen": "Fetch failed",
    "In Wealthfolio stammen {what} dieses ETFs von Finanzfluss, weil die Liste des Anbieters zu wenig davon nennt.":
        "In Wealthfolio, {what} of this ETF come from Finanzfluss, as the issuer's list names too little of them.",
    "Länder und Sektoren": "countries and sectors", "die Länder": "the countries", "die Sektoren": "the sectors",
    "{n} von {m}: {name}": "{n} of {m}: {name}",
    "Alle {n}": "All {n}",
    "ETFs ({n})": "ETFs ({n})",
    "Nur die größten {n} zeigen": "Show only the {n} largest",
    "Alle {n} Positionen von {name} zeigen": "Show all {n} holdings of {name}",
    "Altes Excel-Format (XLS): bitte in Excel als XLSX oder CSV speichern und hochladen.":
        "Old Excel format (XLS): please save it as XLSX or CSV in Excel and upload that.",
    "{name} bei Finanzfluss ansehen": "View {name} on Finanzfluss",
    "Einstellungen der Durchschau": "Look-through settings",
    "Positionslisten automatisch bei den Anbietern abrufen (iShares, Xtrackers) – nach jedem Abruf, je ETF "
    "höchstens wöchentlich. Gesendet wird nur die ISIN bzw. die Produktseite.":
        "Fetch the holdings lists from the issuers automatically (iShares, Xtrackers) – after every fetch, at most "
        "weekly per ETF. Only the ISIN or the product page is sent.",
    "Positionslisten jetzt abrufen": "Fetch holdings lists now",
    # Taxonomien in Wealthfolio (#139)
    "{n} Zuordnungen aus Wealthfolio entfernt.": "{n} assignments removed from Wealthfolio.",
    "Zuletzt in Wealthfolio eingetragen {when}: {written} geändert, {manual} mit deinen eigenen Zuordnungen "
    "gelassen.":
        "Last entered in Wealthfolio {when}: {written} changed, {manual} left with your own assignments.",
    "Aus Wealthfolio entfernen": "Remove from Wealthfolio",
    # FIRE-Rechner (#139)
    "Lean FIRE (75 % der Ausgaben)": "Lean FIRE (75 % of expenses)",
    "FIRE": "FIRE",
    "Fat FIRE (150 % der Ausgaben)": "Fat FIRE (150 % of expenses)",
    "Barista FIRE (mit Teilzeit bis zur Rente)": "Barista FIRE (with part-time work until the pension)",
    "Ausgaben im Jahr (EUR, ohne Krankenversicherung)": "Expenses a year (EUR, without health insurance)",
    "Sparrate im Jahr (EUR)": "Savings a year (EUR)",
    "Vermögen (EUR, leer: aus Wealthfolio)": "Wealth (EUR, empty: from Wealthfolio)",
    "Alter heute": "Age today",
    "Alter bei Rückzug aus dem Beruf": "Age when you stop working",
    "Alter bei Beginn der gesetzlichen Rente": "Age when the statutory pension starts",
    "Rente netto im Jahr (EUR, heutiger Wert)": "Net pension a year (EUR, today's value)",
    "Teilzeit-Einkommen netto im Jahr bis zur Rente (EUR, Barista FIRE)":
        "Net part-time income a year until the pension (EUR, Barista FIRE)",
    "Rendite nach Inflation (% im Jahr)": "Return after inflation (% a year)",
    "Entnahmerate (% im Jahr)": "Withdrawal rate (% a year)",
    "Gewinnanteil eines Verkaufs (%, leer: aus Wealthfolio)": "Gain share of a sale (%, empty: from Wealthfolio)",
    "Kirchensteuer (%: 0, 8 oder 9)": "Church tax (%: 0, 8 or 9)",
    "Private Krankenversicherung im Jahr (EUR)": "Private health insurance a year (EUR)",
    "Zusammenveranlagt (2.000 EUR Sparerpauschbetrag)": "Joint assessment (2,000 EUR saver's allowance)",
    "Freiwillig gesetzlich versichert (sonst privat)": "Voluntarily insured in the statutory scheme (else private)",
    "X-Ray": "X-Ray",
    "Kosten (% im Jahr, leer: aus X-Ray)": "Costs (% a year, empty: from X-Ray)",
    "Zurück zu X-Ray": "Back to X-Ray",
    "weitere ETFs": "other ETFs",
    "direkt": "direct",
    "Summe (EUR)": "Total (EUR)",
    "Gewicht im ETF": "Weight in the ETF",
    "Wert in deinem Depot (EUR)": "Value in your depot (EUR)",
    "Welche Aktien in deinen iShares-ETFs stecken und wie sie sich mit deinen anderen Positionen addieren. Die "
    "Positionslisten holt der Dienst jeden Morgen selbst bei iShares; andere ETFs zählen vorerst als eine Position.":
        "Which stocks your iShares ETFs hold and how they add up with your other positions. The service fetches the "
        "holdings lists from iShares itself every morning; other ETFs count as one position for now.",
    "Gesamt: Aktien über alle Positionen": "Overall: stocks across all positions",
    "Einstellungen dieses ETFs": "Settings of this ETF",
    "Ohne Liste: Such den ETF bei iShares, öffne den Treffer und füg die Adresse seiner Seite hier ein - einmal "
    "genügt.":
        "No list yet: find the ETF at iShares, open the hit and paste its page's address here - once is enough.",
    "Bei iShares suchen ({isin})": "Search at iShares ({isin})",
    "{funds} iShares-ETFs durchleuchtet · {open} ohne Positionsliste · {hidden} weitere Positionen ausgeblendet "
    "(zählen als je eine Position)":
        "{funds} iShares ETFs looked through · {open} without a holdings list · {hidden} other positions hidden "
        "(each counts as one position)",
    "Letzter Abruf {when}: {fetched} Positionslisten geholt, {failed} fehlgeschlagen.":
        "Last fetch {when}: {fetched} holdings lists fetched, {failed} failed.",
    "Der Abruf ist abgebrochen: {error}": "The fetch stopped: {error}",
    "iShares nennt zu einer ISIN gerade keine Produktnummer ({error}). Trag bei einem ETF ohne Liste einmal seine "
    "Produktseite unter „Einstellungen dieses ETFs“ ein - danach holt der Dienst seine Liste selbst.":
        "iShares currently names no product number for an ISIN ({error}). For an ETF without a list, enter its product "
        "page once under “Settings of this ETF” - then the service fetches its list itself.",
    "Zeigen:": "Show:",
    "Nicht zugeordnet (Bargeld, Derivate, Rest der Gewichte): {share}.":
        "Unassigned (cash, derivatives, rest of the weights): {share}.",
    "ohne ISIN": "no ISIN",
    "noch keine Positionsliste": "no holdings list yet",
    "Abruf fehlgeschlagen {when}: {error}": "Fetch failed {when}: {error}",
    "Kein ETF in Wealthfolio gefunden.": "No ETF found in Wealthfolio.",
    ("Dieser Anbieter lässt keinen automatischen Abruf zu: Lade die Positionsliste von der Seite des ETFs herunter "
     "(CSV oder Excel) und hier hoch. Sie gilt, bis du eine neue hochlädst."): (
        "This issuer allows no automatic fetching: download the holdings list from the ETF's page (CSV or Excel) and "
        "upload it here. It counts until you upload a new one."),
    ("Welche Aktien in deinen ETFs stecken und wie sie sich mit deinen Einzelwerten addieren. Die Positionslisten "
     "holt der Dienst jeden Morgen selbst bei den Anbietern (iShares, Xtrackers, SPDR, Amundi, Invesco, Vanguard, "
     "VanEck, ARK)."): (
        "Which stocks are in your ETFs and how they add up with your single holdings. The service fetches the "
        "holdings lists itself every morning from the issuers (iShares, Xtrackers, SPDR, Amundi, Invesco, Vanguard, "
        "VanEck, ARK)."),
    "{funds} ETFs durchleuchtet · {open} ohne Positionsliste · {singles} Einzelwerte": (
        "{funds} ETFs looked through · {open} without a holdings list · {singles} single holdings"),
    "Nicht in der Tabelle, weil ihre Positionsliste noch fehlt: {names}.": (
        "Not in the table because their holdings list is still missing: {names}."),
    ("Synthetischer ETF (Swap): die Liste ist der Wertpapierkorb des Fonds, nicht der Index, dessen Wertentwicklung "
     "er liefert."): ("Synthetic ETF (swap): the list is the fund's basket of securities, not the index whose "
                     "performance it delivers."),
    "Kein iShares-ETF in Wealthfolio gefunden.": "No iShares ETF found in Wealthfolio.",
    "Die Positionen werden beim Öffnen der Seite aus Wealthfolio gelesen, höchstens alle 10 Minuten.":
        "The positions are read from Wealthfolio when the page opens, at most every 10 minutes.",
    "Automatisches Update": "Automatic update",
    "Updates": "Updates",
    "Neue Versionen automatisch installieren": "Install new versions automatically",
    "Wie oft nachsehen (Sekunden)": "How often to look (seconds)",
    "Bitte ganze Sekunden angeben.": "Please give whole seconds.",
    "Installiert: Version {version}.": "Installed: version {version}.",
    "Neueste Version auf GitHub: {version}.": "Newest version on GitHub: {version}.",
    "Eingeschaltet sieht der Container selbst nach, ob eine neue Version erschienen ist, und installiert sie wie "
    "der Befehl update in seiner Konsole: dieselben Prüfungen (jede Datei gegen die Prüfsummen des Releases), "
    "dieselbe Sicherung der Daten vorher. Danach kommt eine Nachricht über ntfy. Schlägt ein Update fehl, läuft "
    "die bisherige Version weiter; dieselbe Version wird nach einer Stunde noch einmal versucht. Während eines "
    "Abrufs wird nicht aktualisiert.":
        "When switched on, the container looks for a new version itself and installs it like the command update "
        "in its console: the same checks (every file against the release's checksums), the same backup of the "
        "data before. Then a message comes via ntfy. If an update fails, the previous version keeps running; the "
        "same version is tried again after an hour. Nothing is updated while a fetch is running.",
    "Mindestens {low} (eine Minute), höchstens {high} (ein Tag). Gefragt wird nur GitHub, nach der neuesten "
    "Version - nichts über dich.":
        "At least {low} (one minute), at most {high} (one day). Only GitHub is asked, for the newest version - "
        "nothing about you.",
    "Letztes automatisches Update": "Last automatic update",
    "{when}: Version {new} installiert (vorher {old}).": "{when}: version {new} installed (before {old}).",
    "{when}: Update auf {new} fehlgeschlagen, Version {old} läuft weiter.":
        "{when}: update to {new} failed, version {old} keeps running.",
    "Klumpenrisiken ({n})": "Cluster risks ({n})",
    "Einstellungen von X-Ray": "X-Ray settings",
    "Positionsliste von {name} jetzt abrufen": "Fetch the holdings list of {name} now",
    "Alle {n} Positionen im Fonds": "All {n} holdings of the fund",
    "Die {top} größten von {n} Positionen im Fonds": "The {top} largest of the fund's {n} holdings",
    "Positionen von {name}": "Holdings of {name}",
    "Gewicht": "Weight",
    "Alle {n} anzeigen": "Show all {n}",
    "Unbekannte Zeichenkodierung - bitte die CSV-Datei des Anbieters hochladen.":
        "Unknown character encoding - please upload the issuer's CSV file.",
    "Keine Positionsliste: Spalten für Name und Gewichtung fehlen.":
        "Not a holdings list: columns for name and weight are missing.",
    "Die Positionsliste enthält keine Zeile mit Gewichtung.": "The holdings list has no line with a weight.",
    "Die XLSX-Datei ist zu groß.": "The XLSX file is too large.",
    "Die XLSX-Datei enthält keine Tabelle.": "The XLSX file contains no sheet.",
    "Keine lesbare XLSX-Datei.": "Not a readable XLSX file.",
    "Keine lesbare Excel-Datei.": "Not a readable Excel file.",
    "Die Datei enthält keine Positionen.": "The file holds no holdings.",
    "In der Rente pflichtversichert (KVdR): kein Beitrag auf Kapitalerträge":
        "Compulsorily insured as a pensioner (KVdR): no contribution on capital income",
    "Kinderlos (höherer Pflegebeitrag)": "Childless (higher long-term care contribution)",
    "Aktienfonds (30 % Teilfreistellung)": "Equity funds (30 % partial exemption)",
    "{label}: bitte eintragen.": "{label}: please fill in.",
    "{label}: bitte eine ganze Zahl.": "{label}: please a whole number.",
    "FIRE-Rechner": "FIRE calculator",
    "Wie viel Vermögen ein Leben von deinem Depot braucht – mit deutscher Abgeltungsteuer, Teilfreistellung, "
    "Sparerpauschbetrag und Krankenversicherung, in heutigem Geld.":
        "How much wealth a life on your portfolio needs – with German flat tax, partial exemption, saver's "
        "allowance and health insurance, in today's money.",
    "Zurück zur Durchschau": "Back to the look-through",
    "Aus Wealthfolio: Vermögen {total} EUR.": "From Wealthfolio: wealth {total} EUR.",
    "Gewinnanteil der Wertpapiere {share} %.": "Gain share of the securities {share} %.",
    "Vereinfachte Modellrechnung, keine Steuer- oder Anlageberatung. Rechtsstand {year}.":
        "Simplified model calculation, no tax or investment advice. Law as of {year}.",
    "Ziel": "Goal",
    "Vermögen nötig": "Wealth needed",
    "Erreicht in": "Reached in",
    "Alter": "Age",
    "nie bei dieser Sparrate": "never at this savings rate",
    "erreicht": "reached",
    "{n} Jahren": "{n} years",
    "Coast FIRE: mit {coast} EUR heute wächst das Depot ohne weiteres Sparen bis zum Rückzug auf das FIRE-Ziel.":
        "Coast FIRE: with {coast} EUR today the portfolio grows to the FIRE goal by the time you stop working, "
        "without saving more.",
    "Erreicht.": "Reached.",
    "Noch nicht erreicht.": "Not reached yet.",
    "Entnahme im Jahr bis zur Rente": "Withdrawal a year until the pension",
    "Posten": "Item",
    "Ausgaben": "Expenses",
    "Steuer auf die Gewinne (Satz {rate} %)": "Tax on the gains (rate {rate} %)",
    "Krankenversicherung": "Health insurance",
    "Entnahme aus dem Depot": "Withdrawal from the portfolio",
    "Deine Angaben": "Your details",
    "Berechnen": "Calculate",
    "Wie wird gerechnet?": "How is it calculated?",
    "Brutto-Entnahme: so viel, dass nach Steuer und Krankenversicherung die Ausgaben bleiben. Steuerpflichtig ist "
    "nur der Gewinnanteil eines Verkaufs, bei Aktienfonds davon 70 % (Teilfreistellung), abzüglich "
    "Sparerpauschbetrag. Bis zur gesetzlichen Rente trägt das Depot die ganze Entnahme (Barwert dieser Jahre), "
    "danach nur, was die Rente offen lässt – als dauerhafte Entnahme zur Entnahmerate. Jahre bis zum Ziel mit "
    "heutigem Vermögen, Sparrate und Rendite nach Inflation. Die Vorabpauschale fehlt: sie wird beim späteren "
    "Verkauf angerechnet.":
        "Gross withdrawal: enough that the expenses remain after tax and health insurance. Only the gain share of a "
        "sale is taxable, for equity funds 70 % of it (partial exemption), less the saver's allowance. Until the "
        "statutory pension the portfolio carries the whole withdrawal (present value of those years), afterwards "
        "only what the pension leaves open – as a permanent withdrawal at the withdrawal rate. Years to the goal "
        "with today's wealth, savings and return after inflation. The advance lump sum (Vorabpauschale) is left "
        "out: it is credited at the later sale.",
    "Werte {year}": "Values {year}",
    "Abgeltungsteuer + Solidaritätszuschlag": "Flat tax + solidarity surcharge",
    "Teilfreistellung Aktienfonds": "Partial exemption of equity funds",
    "Sparerpauschbetrag (einzeln / zusammen)": "Saver's allowance (single / joint)",
    "Kranken- und Pflegeversicherung freiwillig (mit Kind / kinderlos)":
        "Health and care insurance, voluntary (with child / childless)",
    "Mindest- und Höchstbemessung im Jahr": "Minimum and maximum basis a year",
    "FIRE-Rechner: wie viel Vermögen ein Leben vom Depot braucht":
        "FIRE calculator: how much wealth a life on the portfolio needs",
    # Costs (waldonso2/wealthfolio-importer-addon#142)
    "Gesamtkostenquote": "Total cost ratio",
    "Gesamtkostenquote über (% im Jahr)": "Total cost ratio above (% a year)",
    "Ordergebühr je Verkauf (EUR)": "Order fee per sale (EUR)",
    "Ordergebühr je Verkauf (% des Werts)": "Order fee per sale (% of the value)",
    "Spread je Kauf und Verkauf (%)": "Spread per buy and sale (%)",
    "Depotgebühr im Jahr (EUR)": "Custody fee a year (EUR)",
    "Kosten (% im Jahr, leer: aus der Durchschau)": "Costs (% a year, empty: from the look-through)",
    "Laufende Kosten (TER, % im Jahr)": "Ongoing charges (TER, % a year)",
    "Kosten des Depots {rate} % im Jahr.": "Depot costs {rate} % a year.",
    "Ohne Kosten": "Without costs",
    "Gerechnet mit {rate} % Kosten im Jahr, abgezogen von der Rendite nach Inflation.":
        "Calculated with costs of {rate} % a year, taken off the return after inflation.",
    "Ohne Kosten wäre FIRE {n} Jahre früher erreicht.": "Without costs FIRE would be reached {n} years earlier.",
    "Laufende Kosten (TER, % im Jahr) für {name}, leer: vom Anbieter":
        "Ongoing charges (TER, % a year) of {name}, empty: from the issuer",
    "Kosten": "Costs",
    "Gesamtkostenquote {rate} im Jahr": "Total cost ratio {rate} a year",
    "Gesamtkostenquote unbekannt": "Total cost ratio unknown",
    "({share} des Depots ohne bekannte Kostenquote)": "({share} of the depot without a known cost ratio)",
    "Gewichtete TER {ter}.": "Weighted TER {ter}.",
    "Laufend {yearly} EUR im Jahr, bisher angefallen {so_far} EUR.":
        "Ongoing {yearly} EUR a year, incurred so far {so_far} EUR.",
    "Verkauf: {n} Positionen nicht geschätzt (Gebührenmodell fehlt).":
        "Sale: {n} positions not estimated (no fee model).",
    "Verkauf heute geschätzt {exit} EUR.": "Sale today estimated at {exit} EUR.",
    "Steuern (nicht in der Kostenquote): {tax} EUR.": "Taxes (not in the cost ratio): {tax} EUR.",
    "Kosten je Position": "Costs per position",
    "Gehalten": "Held",
    "Ordergebühren": "Order fees",
    "Spread (geschätzt)": "Spread (estimated)",
    "TER": "TER",
    "Laufende Kosten bisher": "Ongoing charges so far",
    "Depotgebühr bisher": "Custody fee so far",
    "Kosten bisher": "Costs so far",
    "Verkauf (geschätzt)": "Sale (estimated)",
    "Steuern": "Taxes",
    "Kostenquote im Jahr": "Cost ratio a year",
    "{n} Jahre": "{n} years",
    "von Hand": "by hand",
    "unbekannt": "unknown",
    "nicht geschätzt": "not estimated",
    "Kostenquote unbekannt": "Cost ratio unknown",
    "Ordergebühren und Steuern stehen so in den Buchungen in Wealthfolio. Geschätzt sind: der Spread (dein "
    "Prozentsatz auf den Wert jedes Kaufs und Verkaufs), die laufenden Kosten (TER mal Mittel aus Einstandswert und "
    "heutigem Wert mal Haltedauer seit dem ersten Kauf) und der Verkauf (dein Gebührenmodell auf den heutigen "
    "Wert). Die Depotgebühr wird nach Wert auf die Positionen des Brokers verteilt. Ohne TER hat eine Position "
    "keine Kostenquote – bei einer Einzelaktie trägst du 0 ein. Die Kostenquote ist alles bisher Angefallene "
    "geteilt durch den mittleren Wert und die Jahre; Steuern zählen nicht dazu. Die Tracking-Differenz fehlt, weil "
    "keine Quelle sie liefert.":
        "Order fees and taxes are as booked in Wealthfolio. Estimated are: the spread (your percentage of the value "
        "of every buy and sale), the ongoing charges (TER times the mean of cost basis and today's value times the "
        "years held since the first buy) and the sale (your fee model on today's value). The custody fee is shared "
        "by value among the broker's positions. Without a TER a position has no cost ratio - for a single stock "
        "enter 0. The cost ratio is everything incurred so far divided by the mean value and the years; taxes are "
        "not part of it. The tracking difference is missing because no source provides it.",
    "Gebühren der Broker": "The brokers' fees",
    "Leer heißt unbekannt: dann wird nichts geschätzt.": "Empty means unknown: then nothing is estimated.",
    # Service pages (#151)
    "{written} Zuordnungen in Wealthfolio eingetragen, {manual} mit deinen eigenen gelassen.":
        "{written} assignments entered in Wealthfolio, {manual} left with your own.",
    "{n} ISINs in Wealthfolio ergänzt.": "{n} ISINs added in Wealthfolio.",
    "Ausgaben-Tracker aktualisiert.": "Spending tracker updated.",
    "Fondslisten abgelegt: {written} geschrieben, {deleted} gelöscht.":
        "Fund lists stored: {written} written, {deleted} deleted.",
    "Fondslisten aus Wealthfolio entfernt.": "Fund lists removed from Wealthfolio.",
    "Erst unten ein Verrechnungskonto wählen.": "First choose a cash account below.",
    "Erst unten einen logo.dev-Schlüssel eintragen.": "First enter a logo.dev key below.",
    "ISINs konnten nicht ergänzt werden - siehe Protokoll.": "ISINs could not be added - see the log.",
    "Der Ausgaben-Tracker wurde nicht aktualisiert - siehe Protokoll.":
        "The spending tracker was not updated - see the log.",
    "Die Fondslisten wurden nicht abgelegt: {error}": "The fund lists were not stored: {error}",
    "Erst einschalten.": "Switch it on first.",
    "Die Fondslisten wurden nicht entfernt: {error}": "The fund lists were not removed: {error}",
    "Bitte zum Entfernen bestätigen.": "Please confirm the removal.",
    "Unbekanntes Konto - bitte die Seite neu laden.": "Unknown account - please reload the page.",
    "Broker und letzter Abruf": "Brokers and last fetch",
    "Zustand": "State",
    "Letzter Abruf": "Last fetch",
    "{when}: {n} neu": "{when}: {n} new",
    "Noch kein Broker eingerichtet.": "No broker set up yet.",
    "Abrufmodus: {mode}": "Fetch mode: {mode}",
    "Sicher – jeder Abruf mit deiner Anmeldung": "Safe – every fetch with your login",
    "Automatisch – der Timer ruft täglich ab": "Automatic – the timer fetches daily",
    "Abrufmodus ändern": "Change fetch mode",
    "Jetzt ausführen": "Run now",
    "Ohne passende Kategorie in Wealthfolio: {names}.": "Without a matching category in Wealthfolio: {names}.",
    "Bewusst nicht zugeordnet (kein Land und keine Branche): {names}.":
        "Deliberately not assigned (no country and no sector): {names}.",
    "Anlageklasse aus dem Fondsnamen (wie Wealthfolio selbst; ersetzt Wealthfolio, sobald ein Kursanbieter die "
    "echte Aufteilung liefert): {names}.":
        "Asset class from the fund's name (as Wealthfolio does itself; Wealthfolio replaces it once a quote provider "
        "gives the real breakdown): {names}.",
    "Anlageklasse aus dem ESMA-Register FIRDS (grob: Aktie, Anleihe, Rohstoff, Fonds nach Hauptanlageart; ersetzt "
    "Wealthfolio, sobald ein Kursanbieter die echte Aufteilung liefert): {names}.":
        "Asset class from ESMA's FIRDS register (rough: share, bond, commodity, fund by principal investment type; "
        "Wealthfolio replaces it once a quote provider gives the real breakdown): {names}.",
    "Anlageklasse beim ESMA-Register nachfragen": "Ask ESMA's register for the asset class",
    "Für eine Position ohne Anlageklasse in Wealthfolio, deren Fondsname sie nicht verrät, fragt der Dienst im "
    "öffentlichen Register FIRDS der europäischen Wertpapieraufsicht (registers.esma.europa.eu) nach dem "
    "Instrumententyp. Nur die ISIN geht dorthin - keine Bestände, Beträge oder Konten. Aktie, Anleihe, Rohstoff, "
    "Immobilie und Fonds nach Hauptanlageart werden zugeordnet; Mischfonds, Derivate und alles, was das Register "
    "nicht kennt, bleiben ohne Anlageklasse. Eine Zuordnung von dir bleibt unangetastet.":
        "For a position without an asset class in Wealthfolio whose fund name doesn't tell, the service asks the "
        "public FIRDS register of the European securities regulator (registers.esma.europa.eu) for the instrument "
        "type. Only the ISIN goes there - no holdings, amounts or accounts. Shares, bonds, commodities, real estate "
        "and funds by principal investment type are assigned; mixed funds, derivatives and anything the register "
        "doesn't know stay without an asset class. An assignment of yours is left alone.",
    "{n} Positionen ohne Anlageklasse in Wealthfolio": "{n} positions without an asset class in Wealthfolio",
    "Ohne Anlageklasse in Wealthfolio: {n}": "Without an asset class in Wealthfolio: {n}",
    "Wealthfolio zählt sie in der Zielallokation unter „Unknown“. Es holt die Aufteilung selbst, sobald das "
    "Wertpapier einen Ticker mit Kursanbieter hat (Wertpapier in Wealthfolio bearbeiten); sonst ordne die "
    "Anlageklasse dort von Hand zu.":
        "Wealthfolio counts them as \u201cUnknown\u201d in the target allocation. It fetches the breakdown itself "
        "once the security has a ticker with a quote provider (edit the security in Wealthfolio); otherwise assign "
        "the asset class there by hand.",
    "Positionen ohne Anlageklasse": "Positions without an asset class",
    "Geprüft {when}.": "Checked {when}.",
    "Noch nichts in Wealthfolio eingetragen.": "Nothing entered in Wealthfolio yet.",
    "Liste des Anbieters": "Issuer's list",
    "Länderaufteilung des Anbieters": "Issuer's country breakdown",
    "Finanzfluss": "Finanzfluss",
    "Quelle der Länder und Sektoren je Fonds": "Source of countries and sectors per fund",
    "Fonds": "Fund",
    "Anbieter": "Issuer",
    "Länder": "Countries",
    "Sektoren": "Sectors",
    "Noch kein Fonds mit Positionsliste - siehe X-Ray.": "No fund with a holdings list yet - see X-Ray.",
    "Ersetzt nur Yahoos automatische Werte und die eigenen des Dienstes, nie deine eigenen Zuordnungen. Was keiner "
    "Kategorie zuzuordnen ist, bleibt nicht zugeordnet.":
        "Replaces only Yahoo's automatic values and the service's own, never your own assignments. What fits no "
        "category stays unassigned.",
    "Jetzt eintragen": "Enter now",
    "Ja, die vom Dienst eingetragenen Länder und Sektoren entfernen und den Dienst ausschalten":
        "Yes, remove the countries and sectors the service entered and switch the service off",
    "Ergänzte ISINs": "Added ISINs",
    "Ergänzt": "Added",
    "Noch keine ISIN ergänzt.": "No ISIN added yet.",
    "Ohne ISIN": "Without an ISIN",
    "Die Positionen werden gerade aus Wealthfolio gelesen.": "The positions are being read from Wealthfolio.",
    "Für diese Wertpapiere kennt der Dienst noch keine ISIN:": "The service knows no ISIN yet for these securities:",
    "Jedes Wertpapier im Depot hat eine ISIN.": "Every security in the depot has an ISIN.",
    "ISIN fehlt? Selbst zuordnen": "ISIN missing? Map it yourself",
    "Jetzt ergänzen": "Add now",
    "Vom Dienst in Wealthfolios Ausgaben-Tracker aufgenommen: {names}.":
        "Added to Wealthfolio's spending tracker by the service: {names}.",
    "Noch kein Konto vom Dienst aufgenommen.": "No account added by the service yet.",
    "Kategorien vergibt Wealthfolio mit seinen Regeln (Ausgaben → Regeln) anhand von Empfänger und "
    "Verwendungszweck. Wealthfolio wertet nur Konten vom Typ Bargeld aus; eigene Überträge und "
    "Wertpapierabrechnungen zählen dort als Sparen, nicht als Ausgabe. Konten, die du in Wealthfolio selbst "
    "gewählt hast, bleiben.":
        "Wealthfolio assigns categories with its rules (Spending → Rules) by payee and purpose. Wealthfolio only "
        "evaluates accounts of type cash; own transfers and securities settlements count as saving there, not as "
        "spending. Accounts you chose in Wealthfolio yourself stay.",
    "Verrechnungskonten im Ausgaben-Tracker": "Cash accounts in the spending tracker",
    "{broker}: {account}": "{broker}: {account}",
    "Noch kein Broker mit Verrechnungskonto.": "No broker with a cash account yet.",
    "Wealthfolio zu alt: der Addon-Speicher kommt erst mit Wealthfolio 3.11.0.":
        "Wealthfolio too old: the addon storage comes with Wealthfolio 3.11.0.",
    "unverändert": "unchanged",
    "{written} geschrieben, {deleted} gelöscht": "{written} written, {deleted} deleted",
    "Noch nichts abgelegt.": "Nothing stored yet.",
    "{n} alte Einträge werden beim nächsten Mal gelöscht.": "{n} old entries are deleted next time.",
    "Fonds mit Liste: {n}": "Funds with a list: {n}",
    "Fonds ohne Liste": "Funds without a list",
    "Grund": "Reason",
    "Addon installieren": "Install the addon",
    "Das Addon rechnet X-Ray in Wealthfolio aus diesen Listen und den Werten deiner Positionen. <a "
    "href=\"https://github.com/waldonso2/wealthfolio-companion-addon-xray/releases/latest\" target=\"_blank\" "
    "rel=\"noopener noreferrer\">Neueste Version herunterladen</a> (die ZIP-Datei), dann in Wealthfolio: "
    "Einstellungen → Add-ons → aus Datei installieren.":
        "The addon computes X-Ray in Wealthfolio from these lists and the values of your positions. <a "
        "href=\"https://github.com/waldonso2/wealthfolio-companion-addon-xray/releases/latest\" target=\"_blank\" "
        "rel=\"noopener noreferrer\">Download the latest version</a> (the ZIP file), then in Wealthfolio: Settings "
        "→ Add-ons → install from file.",
    "Jetzt ablegen": "Store now",
    "Ja, die abgelegten Fondslisten entfernen und den Dienst ausschalten":
        "Yes, remove the stored fund lists and switch the service off",
    "Länder und Sektoren der ETFs in Wealthfolio eintragen: Dienst Länder & Sektoren":
        "Enter the ETFs' countries and sectors in Wealthfolio: service Countries & sectors",
    "Fondslisten für das X-Ray-Addon: Dienst X-Ray in Wealthfolio":
        "Fund lists for the X-Ray addon: service X-Ray in Wealthfolio",
    "Dorthin": "Go there",
    "Aktionen": "Actions",
    "Noch kein Durchlauf.": "No pass yet.",
    "Verrechnungskonto in Wealthfolios Ausgaben-Tracker: Dienst Ausgaben-Tracker":
        "Cash account in Wealthfolio's spending tracker: service Spending tracker",
}
