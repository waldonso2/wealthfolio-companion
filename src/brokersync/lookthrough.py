"""A fund's constituents from its issuer's holdings file (waldonso2/wealthfolio-importer-addon#139).

``parse`` reads a CSV as the issuers publish it - iShares (a few lines of fund details above the header,
weights in percent, no ISIN), or any list with a name and a weight column, in English or German, with
``,``/``;``/tab and decimal point or comma. Each line becomes a ``xray.Constituent``; its key is the ISIN
when there is a valid one, else ticker and exchange, else the name. Weights are a share of the fund
(0..1); what they leave open stays open (``xray.UNASSIGNED``).

``fetch`` gets the file itself (always, #144; ``Syncer.update_funds`` after a run, at most every
``REFRESH_DAYS`` per fund) - for now only iShares (Xtrackers by ISIN, an XLSX, is there but not called), by
its product id - from iShares' fund finders (``ISHARES_SCREENERS``, every product with its ISIN, read once per
pass), from the product page's address the user saves, or, while the fund finder is down, ``ISHARES``. Only
the ISIN or that address leaves the container; only ``HOSTS`` are asked, answers are read up to
``MAX_BYTES`` and must look like a holdings list. Amundi and Vanguard: upload only for now (their
interfaces are unverified, and a swap ETF's list would be its collateral basket, not the index).
"""

from __future__ import annotations

import csv
import html
import io
import json
import logging
import re
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

from . import costs, meta_keys, xray
from .assets import valid_isin
from .logos import isin_of
from .model import when
from .state import State, now
from .wealthfolio import WealthfolioError
from .web.i18n import N_
from .xray import Constituent

log = logging.getLogger(__name__)

WEIGHT = ("weight (%)", "gewichtung (%)", "weighting", "gewichtung", "weight", "anteil (%)", "% of net assets",
          "percent of fund", "constituent weight (base)")
NAME = ("name", "bezeichnung", "emittent", "security name", "security description", "wertpapiere")
ISIN = ("isin",)
TICKER = ("ticker", "emittententicker")
EXCHANGE = ("exchange", "börse")
COUNTRY = ("location", "standort", "country", "land", "sitz", "trade country name", "exposure country")
SECTOR = ("sector", "sektor", "industry classification", "branche")
CURRENCY = ("market currency", "marktwährung", "currency", "währung", "trading currency")


def _text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(N_("Unbekannte Zeichenkodierung - bitte die CSV-Datei des Anbieters hochladen."))


def _column(header: list[str], names: tuple[str, ...]) -> int | None:
    return next((header.index(n) for n in names if n in header), None)


def _number(value: str) -> Decimal | None:
    value = value.strip().replace("%", "").replace("\xa0", "").replace(" ", "")
    if "," in value and "." in value:  # the later one is the decimal mark
        comma_last = value.rfind(",") > value.rfind(".")
        value = value.replace(".", "").replace(",", ".") if comma_last else value.replace(",", "")
    else:
        value = value.replace(",", ".")
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _header(line: str) -> tuple[str, list[str]] | None:
    """Delimiter and column names, if the line is the header (it names a name and a weight column)."""
    delimiter = max(";,\t", key=line.count)
    header = [h.strip().lower() for h in next(csv.reader([line], delimiter=delimiter), [])]
    return (delimiter, header) if _column(header, WEIGHT) is not None and _column(header, NAME) is not None else None


def _key(isin: str, ticker: str, exchange: str, name: str) -> str:
    """A constituent's key: its ISIN when valid, else ticker and exchange, else its name."""
    code = isin.strip().upper()
    # shortcut: without an ISIN, funds of different issuers only meet by ticker or name; OpenFIGI later.
    return code if len(code) == 12 and code.isalnum() and valid_isin(code) else (
        f"{ticker}:{exchange}".upper() if ticker else name.strip().upper())


def records(rows: list[dict], source: str) -> tuple[Constituent, ...]:
    """Constituents from an issuer's JSON rows ({isin, name, weight, country, sector, currency, ticker}); weights
    in percent or as a share, like ``parse``."""
    found = [(r, w) for r in rows if (w := _number(str(r.get("weight") or ""))) and str(r.get("name") or "").strip()]
    scale = Decimal(100) if sum(w for _, w in found) > Decimal("1.5") else Decimal(1)
    lines = tuple(Constituent(_key(str(r.get("isin") or ""), str(r.get("ticker") or ""), "", str(r["name"])),
                              str(r["name"]).strip(), w / scale, str(r.get("country") or ""),
                              str(r.get("sector") or ""), str(r.get("currency") or "")) for r, w in found)
    return _usable(lines, source)


def parse(data: bytes) -> tuple[Constituent, ...]:
    text = _text(data)
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if _header(line)), None)
    if start is None:
        raise ValueError(N_("Keine Positionsliste: Spalten für Name und Gewichtung fehlen."))
    delimiter, header = _header(lines[start]) or (",", [])
    weight, name = _column(header, WEIGHT), _column(header, NAME)
    isin, ticker, exchange = _column(header, ISIN), _column(header, TICKER), _column(header, EXCHANGE)
    country, sector, currency = _column(header, COUNTRY), _column(header, SECTOR), _column(header, CURRENCY)

    def cell(row: list[str], i: int | None) -> str:
        return row[i].strip() if i is not None and i < len(row) else ""

    found: list[tuple[str, str, Decimal, list[str]]] = []
    for row in csv.reader(io.StringIO("\n".join(lines[start + 1:])), delimiter=delimiter):
        w = _number(cell(row, weight))
        if not w or not cell(row, name):  # footer, empty and zero lines
            continue
        found.append((_key(cell(row, isin), cell(row, ticker), cell(row, exchange), cell(row, name)), cell(row, name),
                      w, row))
    if not found:
        raise ValueError(N_("Die Positionsliste enthält keine Zeile mit Gewichtung."))
    # Weights in percent (iShares, most issuers) or already as a share.
    scale = Decimal(100) if sum(w for _, _, w, _ in found) > Decimal("1.5") else Decimal(1)
    return tuple(Constituent(key, label, w / scale, cell(row, country), cell(row, sector), cell(row, currency))
                 for key, label, w, row in found)


# ── fetching ─────────────────────────────────────────────────────────────────
HOSTS = ("www.ishares.com", "www.blackrock.com", "etf.dws.com", "www.ssga.com", "www.amundietf.de",
         "dng-api.invesco.com", "www.vanguard.co.uk", "www.vaneck.com", "europe.ark-funds.com",
         "fundcentres.lgim.com", "fundcentres.landg.com", "hanetf.com", "www.finanzfluss.de")
BROWSER = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
MAX_BYTES = 20 * 1024 * 1024
MAX_COLUMN = 16384  # Excel's last column, XFD
REFRESH_DAYS = 0.75  # daily: the timer runs once a day, so the next day's run must find a list due
NONE_DAYS = 30  # an ISIN no source knows is asked again after this
TER_DAYS = 30  # Finanzfluss is asked for a fund's TER again after this
PAUSE = 1.5  # seconds between two requests to an issuer: iShares answers a burst with HTTP 429
DWS = "https://etf.dws.com/etfdata/export/DEU/DEU/excel/product/constituent/{isin}/"
# SPDR: the fund finder names each fund's ISIN and ticker (in its KID links); the daily holdings are an XLSX by ticker.
SPDR_FINDER = ("https://www.ssga.com/bin/v1/ssmp/fund/fundfinder?country=de&language=de&role=individual&product=etfs"
               "&ui=fund-finder")
SPDR_DOC = "https://www.ssga.com/library-content/products/fund-data/etfs/emea/holdings-daily-emea-en-{ticker}.xlsx"
SPDR_KID = re.compile(r"isin=([A-Z]{2}[A-Z0-9]{10})&[^\"]*?ticker=([a-z0-9]+-[a-z0-9]+)")
# Amundi (and Lyxor): the product API answers a fund's composition by ISIN.
AMUNDI = "https://www.amundietf.de/mapi/ProductAPI/getProductsData"
# Invesco: the holdings of the fund and of its index by ISIN; a swap ETF's own list is its collateral basket, so
# the index's list (the exposure the swap delivers) is taken first.
INVESCO = "https://dng-api.invesco.com/cache/v1/accounts/en_GB/shareclasses/{isin}/holdings/{kind}?idType=isin"
# Vanguard: its product API (GraphQL) by portfolio id - the ISIN's id from the shipped list, else from the API itself.
VANGUARD = "https://www.vanguard.co.uk/gpx/graphql"
VANGUARD_HEADERS = {"X-Consumer-ID": "uk2"}  # the consumer the site's own pages send
VANGUARD_IDS = ('query($portIds: [String!]!) { funds(portIds: $portIds) { portId profile { '
                'identifiers(altIds: ["ISIN"]) { altIdCode altIdValue } } } }')
VANGUARD_HOLDINGS = ('query($portIds: [String!], $lastItemKey: String) { borHoldings(portIds: $portIds) { '
                     'holdings(limit: 1500, lastItemKey: $lastItemKey) { items { issuerName securityLongDescription '
                     'marketValuePercentage ticker bloombergIsoCountry gicsSectorDescription } lastItemKey } } }')
# VanEck: each fund page's holdings block answers JSON by block, page and fund ticker; which fund an ISIN is comes
# from its holdings page (the shipped list, else every fund page of the site's list once a pass).
VANECK = "https://www.vaneck.com"
VANECK_LIST = VANECK + "/de/de/factsheets/etfs/"
VANECK_PAGE = VANECK + "/de/de/anlagen/{slug}/positionen/"
VANECK_HOLDINGS = VANECK + "/Main/HoldingsBlock/GetContent/?blockid={block}&pageid={page}&ticker={ticker}"
# ARK Europe (also the Rize ETFs): each fund page names its ISIN; the full holdings are an XLSX below the page.
ARK = "https://europe.ark-funds.com"
ARK_LIST = ARK + "/de/funds/"
ARK_DOC = ARK + "/funds/{fund}/full-fund-holdings-download/"
ARK_TER = re.compile(r"Gesamtkostenquote</td>\s*<td>\s*(\d+[.,]\d+)\s*%")  # the fund page's facts table
# HANetf: the holdings XLSX lies under the fund's code and ISIN (the code from the shipped list - HANetf's site
# answers automatic requests with a bot check, its files don't).
HANETF_DOC = "https://hanetf.com/wp-content/assets/upload/Holdings-{code}-{isin}-all-all.xlsx"
# Finanzfluss: every ETF's page names its TER in the page's FAQ data ("… hat eine Gesamtkostenquote (TER) von 0,22%") -
# the TER of a fund whose issuer lists none (Xtrackers, Invesco, Vanguard, VanEck, UBS, …), by ISIN.
FINANZFLUSS = "https://www.finanzfluss.de/informer/etf/{isin}/"
FINANZFLUSS_TER = re.compile(r"Gesamtkostenquote \(TER\) von (\d+(?:,\d+)?)\s*%")
# The same page breaks the fund down: countries (ISO code, weight in %) and its own industries (German name, weight).
FINANZFLUSS_COUNTRY = re.compile(r'sr-only[^>]*>([A-Z]{2})</span>.*?<progress[^>]*\svalue="([\d.]+)"', re.S)
FINANZFLUSS_SECTOR = re.compile(r'class="label[^"]*">([^<]+)</div>.*?<progress[^>]*\svalue="([\d.]+)"', re.S)
FALLBACK_BELOW = 5000  # basis points: an issuer's list covering less than half takes Finanzfluss' breakdown
# Finanzfluss' industries -> GICS sector, by the first word part that matches (checked in this order); a name none
# matches stays unassigned and is listed under "Ohne passende Kategorie".
FINANZFLUSS_SECTORS = (
    (("kommunikationsgerät", "halbleiter", "software", "hardware", "it-", "elektron", "technologie"),
     "Information Technology"),
    (("bank", "versicher", "kredit", "finanz", "kapitalm", "börse", "vermögensverwalt"), "Financials"),
    (("pharma", "biotech", "medizin", "gesundheit", "life science", "biowissenschaft"), "Health Care"),
    (("lebensmittel", "getränk", "tabak", "haushalt", "körperpflege", "basiskonsum", "drogerie"), "Consumer Staples"),
    (("fahrzeug", "automob", "zykl", "einzelhandel", "hotel", "restaurant", "freizeit", "bekleid", "luxus",
      "gebrauchsg", "verbraucherdienst"), "Consumer Discretionary"),
    (("medien", "telekom", "unterhaltung", "kommunikation"), "Communication Services"),
    (("baustoff", "chemi", "metall", "bergbau", "rohstoff", "verpack", "papier", "material", "stahl", "gold"),
     "Materials"),
    (("öl", "gas", "energie", "kohle"), "Energy"),
    (("versorg", "strom", "wasser"), "Utilities"),
    (("immobil", "reit"), "Real Estate"),
    (("industrie", "luft", "raumfahrt", "verteidigung", "maschinen", "transport", "logistik", "bau", "elektrische",
      "investitionsg", "gewerbl", "dienstleist"), "Industrials"))
HANETF_LIST = "https://hanetf.com/wp-content/assets/upload/productlist-all-all-all-all.xlsx"  # ISIN, TER (a share)
# The product page's "Daten herunterladen" (10/2026): an Excel 2003 XML workbook of the fund, by product number;
# the holdings are its sheet "Holdings". The page's old CSV address answers with the page itself since then.
ISHARES_DOC = ("https://www.blackrock.com/varnish-api/uk-retail01-product-data/product-data/api/v1/get-fund-document"
               "?appType=PRODUCT_PAGE&appSubType=ISHARES&targetSite=de-ishares-v2&locale=de_DE&portfolioId={id}"
               "&component=fundDownloadV2&userType=individual")
# The fund finder's data: {portfolioId: {isin, productPageUrl, fundName, ...}} for every product the country's site
# sells. A product has the same number on every site; since the German and UK sites were rebuilt (10/2026) only some
# countries still answer, and each lists other funds - asked in turn until one knows the ISIN. The German site's new
# product list reads the same rows from a screener server (first: it also lists the German-domiciled (DE) funds).
ISHARES_SCREENERS = (
    "https://www.ishares.com/varnish-api/blk-product-screener-server/api/v1/product-screener/product-data"
    "?country=de&language=de&siteName=de-ishares-v2&userType=individual",
    *(f"https://www.ishares.com/{site}/product-screener/product-screener-v3.1.jsn?dcrPath=/templatedata/config/"
    f"product-screener-v3/data/{data}/product-screener/ishares-product-screener-backend-config"
    "&siteEntryPassthrough=true"
    for site, data in (("nl/particuliere-belegger/nl", "nl/nl"), ("it/investitore-privato/it", "it/it"),
                       ("es/inversor-particular/es", "es/es"), ("uk/individual/en", "en/uk"))))
# The site's search box: [{category, id, label}]; a product's id holds its page or its number. Used when the fund
# finder fails - it answers per ISIN.
# The UK site's first, then the German one's (it lists funds the UK site doesn't); a product is the same number on both.
ISHARES_SEARCH = tuple(f"https://www.ishares.com/{site}/autoComplete.search"
                       "?type=autocomplete&siteEntryPassthrough=true&term={isin}"
                       for site in ("uk/individual/en", "de/privatanleger/de"))
# The page's name after the number is optional: links often end at the number (.../produkte/308858/).
PRODUCT_PATH = re.compile(r"/(?:products|produkte)/(\d{4,8})(?:/([a-z0-9-]{3,120}))?")
ISHARES_PAGE = re.compile(r"^https://www\.ishares\.com/[a-z]{2}/[a-z-]+/[a-z]{2}/(?:products|produkte)/(\d{4,8})"
                          r"(?:/([a-z0-9-]{3,120}))?/?(?:[?#].*)?$")
# Only while the fund finder is down: product ids confirmed by another project's live check (dmxch/openfolio,
# 2026-06). Never a guess - a wrong id can be another fund's list.
ISHARES = {
    "IE00B4L5Y983": ("251882", "ishares-msci-world-ucits-etf-acc-fund"),
    "IE00BKM4GZ66": ("264659", "ishares-msci-emerging-markets-imi-ucits-etf"),
}


class SourceError(Exception):
    """The source answered, but not with a usable holdings list; the text is shown on the page."""


class NoSource(Exception):
    """No source knows this ISIN (a stock, or a fund of another issuer): upload only."""


# L&G: a fund's daily holdings CSV ("Fundholdings.csv") keeps its document id; the user saves its address once.
LGIM_DOC = re.compile(r"^https://fundcentres\.(?:lgim|landg)\.com/srp/documents-id/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                      r"[0-9a-f]{4}-[0-9a-f]{12}/[A-Za-z0-9._-]{1,80}$")


def lgim_doc(url: str) -> bool:
    return bool(LGIM_DOC.match(url.strip()))


def ishares_page(url: str) -> tuple[str, str] | None:
    """Product id and name in the address of an iShares product page, if it is one."""
    m = ISHARES_PAGE.match(url.strip())
    return _product(m) if m else None


def _product(m: re.Match) -> tuple[str, str]:
    # Any name serves: the export finds the list by the number (``holdings`` like the reference implementation).
    return m.group(1), m.group(2) or "holdings"


def xlsx_rows(data: bytes) -> list[list[str]]:
    """The first sheet of an XLSX as rows of text - enough for a holdings export, no styles or formulas."""
    try:
        book = zipfile.ZipFile(io.BytesIO(data))
        if sum(i.file_size for i in book.infolist()) > 2 * MAX_BYTES:  # a zip bomb, not a holdings list
            raise SourceError(N_("Die XLSX-Datei ist zu groß."))
        names = book.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            # Python's expat refuses entity expansion attacks; ElementTree resolves no external entities.
            root = ElementTree.fromstring(book.read("xl/sharedStrings.xml"))  # noqa: S314 - see above
            shared = ["".join(t.text or "" for t in si.iter() if t.tag.endswith("}t")) for si in root
                      if si.tag.endswith("}si")]
        sheet = next((n for n in sorted(names) if n.startswith("xl/worksheets/sheet")), None)
        if sheet is None:
            raise SourceError(N_("Die XLSX-Datei enthält keine Tabelle."))
        root = ElementTree.fromstring(book.read(sheet))  # noqa: S314 - see above
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError) as e:
        raise SourceError(N_("Keine lesbare XLSX-Datei.")) from e
    rows = []
    for row in (e for e in root.iter() if e.tag.endswith("}row")):
        cells: dict[int, str] = {}
        for c in (e for e in row if e.tag.endswith("}c")):
            column = 0
            for ch in re.match(r"[A-Z]*", c.get("r", "")).group(0)[:3]:  # type: ignore[union-attr]
                column = column * 26 + ord(ch) - 64
            if column > MAX_COLUMN:  # beyond Excel's last column: no list of ours, and a row that big is a bomb
                continue
            value = next((v.text or "" for v in c.iter() if v.tag.endswith("}v")), "")
            if c.get("t") == "s":
                value = shared[int(value)] if value.isdigit() and int(value) < len(shared) else ""
            elif c.get("t") == "inlineStr":
                value = "".join(t.text or "" for t in c.iter() if t.tag.endswith("}t"))
            cells[(column or len(cells) + 1) - 1] = value
        rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
    return rows


SS = "{urn:schemas-microsoft-com:office:spreadsheet}"


def workbook_rows(data: bytes, sheet: str) -> list[list[str]]:
    """One sheet of an Excel 2003 XML workbook (SpreadsheetML) as rows of text."""
    text = _text(data)
    text = re.sub(r"&(?!#?\w+;)", "&amp;", text)  # a bare & in a fund's name would end the parse
    try:
        root = ElementTree.fromstring(text)  # noqa: S314 - no entities resolved (see xlsx_rows)
    except ElementTree.ParseError as e:
        raise SourceError(N_("Keine lesbare Excel-Datei.")) from e
    table = next((w for w in root.iter(f"{SS}Worksheet") if w.get(f"{SS}Name") == sheet), None)
    if table is None:
        raise SourceError(N_("Die Datei enthält keine Positionen."))
    rows = []
    for row in table.iter(f"{SS}Row"):
        cells: list[str] = []
        for cell in row.iter(f"{SS}Cell"):
            index = cell.get(f"{SS}Index")
            if index and index.isdigit() and int(index) <= MAX_COLUMN:
                cells += [""] * (int(index) - 1 - len(cells))
            data_ = cell.find(f"{SS}Data")
            cells.append("".join(data_.itertext()) if data_ is not None else "")
        rows.append(cells)
    return rows


def _rows_to_csv(rows: list[list[str]]) -> bytes:
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    return out.getvalue().encode()


def locations(rows: list[list[str]]) -> list[list[str]]:
    """iShares' "Geography/Locations" block: [[ISO code, weight as a share]] - "Other" and cash have no code."""
    start = next((i for i, r in enumerate(rows) if r[:1] == ["Geography/Locations"]), None)
    out: list[list[str]] = []
    for row in rows[start + 1:] if start is not None else []:
        if not row:
            if out:
                break
            continue
        if len(row) > 2 and re.fullmatch(r"[A-Z]{2}", row[2].strip()) and (w := _number(row[1])) is not None:
            out.append([row[2].strip(), str(w / 100)])
    return out


def finanzfluss_page(page: str) -> dict:
    """TER, countries and industries of a Finanzfluss ETF page; the industries as GICS sectors where one fits."""
    def block(start: str, end: str) -> str:
        i = page.find(start)
        j = page.find(end, i + 1) if i >= 0 else -1
        return page[i:j] if i >= 0 and j > i else ""

    ter = _ter({"ter": m.group(1)}) if (m := FINANZFLUSS_TER.search(page)) else None
    regions = [[code, str(Decimal(w) / 100)] for code, w in FINANZFLUSS_COUNTRY.findall(block(">Länder", ">Regionen"))]
    sectors = [[_gics(html.unescape(name).strip()), str(Decimal(w) / 100)]
               for name, w in FINANZFLUSS_SECTOR.findall(block(">Branchen", ">Anlageklassen"))]
    return {"ter": ter, "regions": regions, "sectors": sectors}


def _gics(name: str) -> str:
    lower = name.lower()
    return next((sector for parts, sector in FINANZFLUSS_SECTORS if any(p in lower for p in parts)), name)


def parse_file(data: bytes) -> tuple[Constituent, ...]:
    """A holdings file as CSV or XLSX (also when it is named .xls, like UBS' download)."""
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":  # the binary Excel 97 format (OLE2)
        raise SourceError(N_("Altes Excel-Format (XLS): bitte in Excel als XLSX oder CSV speichern und hochladen."))
    if data[:2] != b"PK":
        return parse(data)
    return parse(_rows_to_csv(xlsx_rows(data)))


def _usable(lines: tuple[Constituent, ...], source: str) -> tuple[Constituent, ...]:
    total = sum((c.weight for c in lines), Decimal(0))
    if len(lines) < 5 or not Decimal("0.5") <= total <= Decimal("1.1"):
        raise SourceError(f"{source}: unvollständige Positionsliste ({len(lines)} Zeilen, {total * 100:.0f} %).")
    return lines


# The fields that carry the TER in percent (#142): iShares' screener has "ter" ("-" when empty) and "ter_ocf", SPDR's
# fund finder "ter" as ["0,30%", 0.3], Amundi's product API the characteristic "TER".
TER_FIELDS = ("ter", "ter_ocf", "onGoingCharges", "fees", "totalExpenseRatio")


def _ter(row: dict) -> Decimal | None:
    """The TER of a fund finder row as a share, if one of ``TER_FIELDS`` holds a plausible percentage."""
    for key in TER_FIELDS:
        value = row.get(key)
        if isinstance(value, dict):  # {"r": 0.2, "d": "0.20"}: raw and display value
            value = value.get("r", value.get("raw", value.get("d")))
        if isinstance(value, list):  # ["0,30%", 0.3]: display and raw value
            value = value[-1] if value else None
        try:
            number = Decimal(str(value).replace("%", "").replace(",", ".").strip())
        except InvalidOperation:
            continue
        if number.is_finite() and 0 <= number <= 3:
            return number / 100
    return None


def _raw(value):
    """The fund finder's value: plain, or {"d": shown, "r": raw} like its numbers."""
    return (value.get("r") or value.get("d")) if isinstance(value, dict) else value


def _only_sources(request: httpx.Request) -> None:
    if request.url.scheme != "https" or request.url.host not in HOSTS:
        raise httpx.UnsupportedProtocol(f"Anfrage an {request.url.host} abgelehnt")


def finder_rows(rows: object) -> Iterator[tuple[str, tuple[str, str], dict]]:
    """ISIN, product (number, page name) and the row of every product in a fund finder's answer: the number from
    the page link, else ``portfolioId``, else the list's key."""
    for key, row in rows.items() if isinstance(rows, dict) else ():
        code = _raw(row.get("isin")) if isinstance(row, dict) else None
        if not isinstance(code, str):
            continue
        m = PRODUCT_PATH.search(str(_raw(row.get("productPageUrl")) or ""))
        number = str(_raw(row.get("portfolioId")) or key)
        if m or re.fullmatch(r"\d{4,8}", number):
            yield code.strip().upper(), _product(m) if m else (number, "holdings"), row


# ISIN -> product number of every iShares fund the fund finders listed, shipped with the release
# (scripts/ishares_products.py, renewed weekly by .github/workflows/ishares-products.yml): no container has to
# read the finders' megabytes for a fund in it. A number never changes, so the file only grows.
PRODUCTS_FILE = Path(__file__).with_name("ishares_products.json")
KNOWN: dict[str, str] = json.loads(PRODUCTS_FILE.read_text()) if PRODUCTS_FILE.exists() else {}
# The same for other issuers whose source needs an id of their own: {issuer: {ISIN: id}} (Vanguard's portfolio ids).
FUNDS_FILE = Path(__file__).with_name("fund_products.json")
PRODUCTS: dict[str, dict[str, str]] = json.loads(FUNDS_FILE.read_text()) if FUNDS_FILE.exists() else {}


class Sources:
    def __init__(self, *, transport: httpx.BaseTransport | None = None, timeout: float = 60.0):
        # Redirects only to the sources themselves; a browser's name, or the issuers' CDN refuses.
        self._http = httpx.Client(timeout=timeout, transport=transport, follow_redirects=True,
                                  headers={"User-Agent": BROWSER, "Accept": "*/*"},
                                  event_hooks={"request": [_only_sources]})
        self._ishares: dict[str, tuple[str, str]] | None = None
        self._pending: list[str] = []
        self._finder_ok = False  # one fund finder answered with products this pass
        self._ter: dict[str, Decimal] = {}
        self._country: dict[str, str] = {}  # iShares' fund finder: the one country a fund invests in
        self.finder_error = ""  # why the fund finder gave no products this pass: an unknown ISIN may still be iShares'
        self.limited = ""  # iShares said "too many requests" (429): nothing more is asked this pass
        self.last: tuple[str, str] | None = None  # the product the last fetch found, kept with the fund
        self.note = ""  # what the last list is, if it is no plain holdings list (a swap ETF's basket)
        self.regions: list[list[str]] = []  # the last fund's countries as its issuer breaks them down: [[code, w]]
        self._spdr: dict[str, str] | None = None
        self._vanguard: dict[str, str] | None = None
        self._vaneck: dict[str, str] | None = None
        self._ark: dict[str, str] | None = None
        self._hanetf_ter = False
        self._sent = 0.0

    @property
    def finder_down(self) -> bool:
        return bool(self.finder_error)

    def ishares_product(self, isin: str, *, search: bool = True) -> tuple[str, str] | None:
        """Product number and page name of the iShares fund with ``isin``, from the fund finders (each read at most
        once a pass, the next only while the ISIN is still unknown)."""
        if isin in KNOWN:
            return KNOWN[isin], "holdings"
        if self._ishares is None:
            self._ishares, self._pending = {}, list(ISHARES_SCREENERS)
        while isin not in self._ishares and self._pending:
            self._read_finder(self._pending.pop(0), self._ishares)
        if isin in self._ishares or not self.finder_down:
            return self._ishares.get(isin)
        return ISHARES.get(isin) or (self._search(isin) if search else None)

    def _read_finder(self, url: str, known: dict[str, tuple[str, str]]) -> None:
        rows: object = None
        try:
            data = self._get(url, "iShares-Fondsfinder")
            rows = json.loads(data) if data is not None else None
            error = "" if data is not None else "iShares-Fondsfinder: HTTP 404"
        except SourceError as e:
            error = str(e)
        except ValueError:
            error = "iShares-Fondsfinder: Antwort ist kein JSON (vermutlich eine Zwischenseite)."
        found = 0
        for code, product, row in finder_rows(rows):
            if not known:  # names only, no values: which field holds the TER (#142)
                log.info("iShares fund finder: fields of a product: %s", ", ".join(sorted(map(str, row))))
            known.setdefault(code, product)
            if (ter := _ter(row)) is not None:
                self._ter.setdefault(code, ter)
            if (country := str(row.get("aladdinCountry") or "").strip()) and country != "Weitere":
                self._country.setdefault(code, country)  # a single-country fund's country ("Indien")
            found += 1
        if found:
            self._finder_ok, self.finder_error = True, ""
        elif not self._finder_ok:
            if not error:
                first = next(iter(rows.values()), None) if isinstance(rows, dict) else None
                log.warning("iShares fund finder: no products; answer %s, first %s: %s", type(rows).__name__,
                            type(first).__name__, ", ".join(sorted(map(str, first)))[:500]
                            if isinstance(first, dict) else "-")
                error = ("iShares-Fondsfinder: Antwort ohne Produkte mit ISIN und Produktseite "
                         f"({len(rows) if isinstance(rows, dict | list) else 0} Einträge).")
            self.finder_error = error

    def _search(self, isin: str) -> tuple[str, str] | None:
        """The product of ``isin`` from the site's search box - only an unambiguous hit, never a guess. The UK
        site first, then the German one; an error only when neither answered."""
        error = None
        for url in ISHARES_SEARCH:
            try:
                found = self._hits(url.format(isin=quote(isin, safe="")))
            except SourceError as e:
                error = e
                continue
            if found:
                return found.pop() if len({p for p, _ in found}) == 1 else None
            error = None
        if error:
            raise error
        return None

    def _hits(self, url: str) -> set[tuple[str, str]]:
        data = self._get(url, "iShares-Suche")
        try:
            rows = json.loads(data or b"[]")
        except ValueError as e:
            raise SourceError("iShares-Suche: Antwort ist kein JSON (vermutlich eine Zwischenseite).") from e
        found = set()
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, dict) and row.get("category") == "productAutocomplete":
                text = str(row.get("id") or "")
                if m := PRODUCT_PATH.search(text):
                    found.add(_product(m))
                elif re.fullmatch(r"\d{4,8}", text):
                    found.add((text, "holdings"))
        return found

    def _finder_facts(self, isin: str) -> None:
        """A fund found in the shipped list skipped the fund finder; its TER and country are read there once a
        pass (the German screener: it lists every fund the shipped list has)."""
        if isin in KNOWN and self._ishares is None:
            self._ishares, self._pending = {}, list(ISHARES_SCREENERS[1:])
            self._read_finder(ISHARES_SCREENERS[0], self._ishares)

    def fund_country(self, isin: str) -> list[list[str]]:
        """A single-country fund's whole weight in its country, as its issuer names it (else nothing)."""
        self._finder_facts(isin)
        return [[self._country[isin], "1"]] if isin in self._country else []

    def ter(self, isin: str) -> Decimal | None:
        """The TER the issuer lists for ``isin``, as far as a fetch of this pass read it."""
        self._finder_facts(isin)
        return self._ter.get(isin)

    def close(self) -> None:
        self._http.close()

    def _get(self, url: str, source: str, body: dict | None = None, headers: dict | None = None) -> bytes | None:
        """The body, or None for 404; read no more than ``MAX_BYTES``. Requests are spaced by ``PAUSE``; after a
        429 none is sent this pass."""
        if self.limited:
            raise SourceError(self.limited)
        time.sleep(max(0.0, self._sent + PAUSE - time.monotonic()))
        self._sent = time.monotonic()
        try:
            with self._http.stream("POST" if body else "GET", url, json=body, headers=headers) as r:
                if r.status_code == 404:
                    return None
                if r.status_code == 429:
                    self.limited = (f"{source}: zu viele Anfragen (HTTP 429) - beim nächsten Abruf wird wieder "
                                    "gefragt.")
                    raise SourceError(self.limited)
                if r.status_code >= 400:
                    raise SourceError(f"{source}: HTTP {r.status_code}")
                data = bytearray()  # not bytes +=: that copies the whole buffer for every chunk
                for chunk in r.iter_bytes():
                    data += chunk
                    if len(data) > MAX_BYTES:
                        raise SourceError(f"{source}: Antwort größer als {MAX_BYTES // 1024 // 1024} MB")
                return bytes(data)
        except httpx.HTTPError as e:
            raise SourceError(f"{source} ist nicht erreichbar ({type(e).__name__}).") from e

    def ishares(self, product: str, slug: str) -> tuple[Constituent, ...]:
        """The holdings of iShares product ``product`` (its number; ``slug`` is not needed any more)."""
        data = self._get(ISHARES_DOC.format(id=product), "iShares") or b""
        workbook = b"Workbook" in data[:4096]
        if not workbook and data.lstrip()[:1] in (b"", b"<"):  # nothing, or an HTML page: no such product
            raise SourceError(f"iShares: keine Positionsliste zu Produkt {product}.")
        try:
            rows = parse(_rows_to_csv(workbook_rows(data, "Holdings"))) if workbook else parse(data)
            lines = _usable(rows, "iShares")
        except ValueError as e:
            raise SourceError(f"iShares: {e}") from e
        try:  # the holdings name no country; broad funds break their countries down on a sheet of their own
            self.regions = locations(workbook_rows(data, "Exposure Breakdowns")) if workbook else []
        except SourceError:
            self.regions = []
        return lines

    def document(self, url: str, source: str) -> tuple[Constituent, ...]:
        """A holdings file at an address the user saved (L&G)."""
        data = self._get(url, source)
        if not data or data.lstrip()[:1] == b"<":
            raise SourceError(f"{source}: keine Positionsliste unter der gespeicherten Adresse.")
        return self._file(data, source)

    def _file(self, data: bytes, source: str) -> tuple[Constituent, ...]:
        try:
            return _usable(parse_file(data), source)
        except ValueError as e:
            raise SourceError(f"{source}: {e}") from e

    def xtrackers(self, isin: str) -> tuple[Constituent, ...] | None:
        data = self._get(DWS.format(isin=isin), "Xtrackers")
        if not data or data[:2] != b"PK":  # no XLSX: not one of theirs
            return None
        return self._file(data, "Xtrackers")

    def spdr(self, isin: str) -> tuple[Constituent, ...] | None:
        if self._spdr is None:  # the fund finder once a pass: ISIN -> ticker
            finder = (self._get(SPDR_FINDER, "SPDR-Fondsfinder") or b"").decode(errors="replace")
            self._spdr = dict(SPDR_KID.findall(finder))
            try:
                funds = json.loads(finder)["data"]["funds"]["etfs"]["datas"]
            except (ValueError, KeyError, TypeError):
                funds = []
            for row in funds if isinstance(funds, list) else []:
                if isinstance(row, dict) and (ter := _ter(row)) is not None:
                    for code, _ticker in SPDR_KID.findall(json.dumps(row)):
                        self._ter.setdefault(code, ter)
        if not (ticker := self._spdr.get(isin)):
            return None
        data = self._get(SPDR_DOC.format(ticker=ticker), "SPDR")
        if not data or data[:2] != b"PK":
            raise SourceError(f"SPDR: keine Positionsliste zu {ticker.upper()}.")
        return self._file(data, "SPDR")

    def amundi(self, isin: str) -> tuple[Constituent, ...] | None:
        body = {"context": {"countryCode": "DEU", "languageCode": "de", "userProfileName": "RETAIL"},
                "productIds": [isin], "productType": "PRODUCT",
                "characteristics": ["FUND_REPLICATION_METHODOLOGY", "TER"],
                "composition": {"compositionFields": ["isin", "name", "weight", "currency", "sector", "countryOfRisk",
                                                      "bbg"]}}
        try:
            product = (json.loads(self._get(AMUNDI, "Amundi", body) or b"{}").get("products") or [{}])[0]
        except ValueError as e:
            raise SourceError("Amundi: Antwort ist kein JSON.") from e
        rows = [{**r.get("compositionCharacteristics", {}), "weight": r.get("weight")}
                for r in (product.get("composition") or {}).get("compositionData") or []]
        if not rows:
            return None
        characteristics = product.get("characteristics") or {}
        if "swap" in str(characteristics.get("FUND_REPLICATION_METHODOLOGY", "")).lower():
            self.note = "swap"
        if (ter := _ter({"ter": characteristics.get("TER")})) is not None:
            self._ter.setdefault(isin, ter)
        return records([{**r, "country": r.get("countryOfRisk"), "ticker": r.get("bbg")} for r in rows], "Amundi")

    def _graphql(self, query: str, variables: dict) -> dict:
        try:
            answer = json.loads(self._get(VANGUARD, "Vanguard", {"query": query, "variables": variables},
                                          VANGUARD_HEADERS) or b"{}")
        except ValueError as e:
            raise SourceError("Vanguard: Antwort ist kein JSON.") from e
        return answer.get("data") or {}

    def vanguard_port(self, isin: str) -> str | None:
        if port := PRODUCTS.get("Vanguard", {}).get(isin):
            return port
        if self._vanguard is None:  # not shipped yet: every id once a pass
            self._vanguard = self.vanguard_ids()
        return self._vanguard.get(isin)

    def vanguard_ids(self) -> dict[str, str]:
        """ISIN -> portfolio id of every Vanguard fund: the ids are four digits, asked a thousand at a time."""
        found: dict[str, str] = {}
        for start in range(0, 10000, 1000):
            for f in self._graphql(VANGUARD_IDS, {"portIds": [f"{n:04d}" for n in range(start, start + 1000)]}
                                   ).get("funds") or []:
                for i in (f.get("profile") or {}).get("identifiers") or []:
                    if valid_isin(str(i.get("altIdValue") or "")):
                        found.setdefault(str(i["altIdValue"]), str(f.get("portId")))
        return found

    def vanguard(self, isin: str) -> tuple[Constituent, ...] | None:
        if not (port := self.vanguard_port(isin)):
            return None
        items: list[dict] = []
        key = None
        for _ in range(20):  # pages of 1500 lines
            page = ((self._graphql(VANGUARD_HOLDINGS, {"portIds": [port], "lastItemKey": key}).get("borHoldings")
                     or [{}])[0] or {}).get("holdings") or {}
            items += page.get("items") or []
            if not (key := page.get("lastItemKey")):
                break
        return records([{"name": r.get("issuerName") or r.get("securityLongDescription"),
                         "weight": r.get("marketValuePercentage"), "ticker": r.get("ticker"),
                         "country": r.get("bloombergIsoCountry"), "sector": r.get("gicsSectorDescription")}
                        for r in items], "Vanguard") if items else None

    def vaneck_ids(self) -> dict[str, str]:
        """ISIN -> "block:page:ticker" of every VanEck fund whose holdings page names its ISIN."""
        found: dict[str, str] = {}
        listing = (self._get(VANECK_LIST, "VanEck") or b"").decode(errors="replace")
        for slug in sorted(set(re.findall(r'href="/de/de/anlagen/([a-z0-9-]+)/"', listing))):
            page = (self._get(VANECK_PAGE.format(slug=slug), "VanEck") or b"").decode(errors="replace")
            block = re.search(r'<ve-holdingsblock[^>]*data-blockid="(\d+)"[^>]*data-pageid="(\d+)"', page)
            ticker = re.search(r"<ve-fundticker[^>]*>([A-Z0-9]+)<", page)
            if block and ticker:
                for code in set(re.findall(r"\b(?:IE|NL|LU)[A-Z0-9]{9}\d\b", page)):
                    if valid_isin(code):
                        found.setdefault(code, f"{block[1]}:{block[2]}:{ticker[1]}")
        return found

    def vaneck(self, isin: str) -> tuple[Constituent, ...] | None:
        if not (fund := PRODUCTS.get("VanEck", {}).get(isin)):
            if self._vaneck is None:
                self._vaneck = self.vaneck_ids()
            if not (fund := self._vaneck.get(isin)):
                return None
        block, page, ticker = fund.split(":")
        try:
            data = json.loads(self._get(VANECK_HOLDINGS.format(block=block, page=page, ticker=ticker), "VanEck")
                              or b"{}").get("data") or {}
        except ValueError as e:
            raise SourceError("VanEck: Antwort ist kein JSON.") from e
        rows = data.get("Holdings") or []
        return records([{"name": r.get("HoldingName"), "weight": r.get("Weight"), "ticker": r.get("Label"),
                         "country": r.get("Country"), "sector": r.get("Sector"), "currency": r.get("CurrencyCode")}
                        for r in rows], "VanEck") if rows else None

    def ark_ids(self) -> dict[str, str]:
        """ISIN -> "<kind>/<slug>" of every fund on ARK Europe's fund list."""
        found: dict[str, str] = {}
        listing = (self._get(ARK_LIST, "ARK") or b"").decode(errors="replace")
        for fund in sorted(set(re.findall(r'href="/de/funds/([a-z-]+/[a-z0-9-]+)/"', listing))):
            page = (self._get(f"{ARK_LIST}{fund}/", "ARK") or b"").decode(errors="replace")
            for code in set(re.findall(r"\bIE[A-Z0-9]{9}\d\b", page)):
                if valid_isin(code):
                    found.setdefault(code, fund)
        return found

    def ark(self, isin: str) -> tuple[Constituent, ...] | None:
        if not (fund := PRODUCTS.get("ARK", {}).get(isin)):
            if self._ark is None:
                self._ark = self.ark_ids()
            if not (fund := self._ark.get(isin)):
                return None
        data = self._get(ARK_DOC.format(fund=fund), "ARK")
        if not data or data[:2] != b"PK":
            raise SourceError(f"ARK: keine Positionsliste zu {fund}.")
        lines = self._file(data, "ARK")
        try:
            page = (self._get(f"{ARK_LIST}{fund}/", "ARK") or b"").decode(errors="replace")
        except SourceError:  # the TER only: the list stands without it
            page = ""
        if (m := ARK_TER.search(page)) and (ter := _ter({"ter": m.group(1)})) is not None:
            self._ter.setdefault(isin, ter)
        return lines

    def finanzfluss(self, isin: str) -> dict:
        """The fund's page at Finanzfluss: {ter, regions: [[code, w]], sectors: [[GICS sector, w]]}."""
        return finanzfluss_page((self._get(FINANZFLUSS.format(isin=isin.lower()), "Finanzfluss") or b"")
                                .decode(errors="replace"))

    def hanetf_ters(self) -> None:
        """Every HANetf fund's TER from its product list (once a pass; the TER column is a share, not percent)."""
        try:
            data = self._get(HANETF_LIST, "HANetf") or b""
            table = xlsx_rows(data) if data[:2] == b"PK" else []
        except SourceError:
            table = []
        header = next((i for i, r in enumerate(table) if "ISIN" in r and "TER" in r), None)
        if header is None:
            return
        isin, share = table[header].index("ISIN"), table[header].index("TER")
        for row in table[header + 1:]:
            number = _number(row[share]) if len(row) > max(isin, share) else None
            if number is not None and (ter := _ter({"ter": number * 100})) is not None:
                self._ter.setdefault(row[isin], ter)

    def hanetf(self, isin: str) -> tuple[Constituent, ...] | None:
        # shortcut: only the shipped codes - a fund HANetf launches later needs the list renewed (its product list
        # sits behind the bot check)
        if not (code := PRODUCTS.get("HANetf", {}).get(isin)):
            return None
        if not self._hanetf_ter:
            self._hanetf_ter = True
            self.hanetf_ters()
        data = self._get(HANETF_DOC.format(code=code, isin=isin), "HANetf")
        if not data or data[:2] != b"PK":
            raise SourceError(f"HANetf: keine Positionsliste zu {code}.")
        return self._file(data, "HANetf")

    def invesco(self, isin: str) -> tuple[Constituent, ...] | None:
        error: SourceError | None = None
        for kind in ("index", "fund"):
            try:
                rows = json.loads(self._get(INVESCO.format(isin=isin, kind=kind), "Invesco") or b"{}").get("holdings")
            except SourceError as e:  # an ISIN Invesco doesn't know answers 500
                error = e
                continue
            except ValueError:
                rows = None
            if rows:
                return records(rows, "Invesco")
        if error and "HTTP 5" not in str(error):
            raise error
        return None


def fetch(sources: Sources, isin: str, page: str = "", *, product: tuple[str, str] | None = None,
          search: bool = True, by: str = "") -> tuple[tuple[Constituent, ...], str]:
    """The holdings of the fund with ``isin`` (or at the iShares product ``page``, or the ``product`` found before)
    and the source's name. ``by``: the issuer the name says - its own source is asked first. ``search``: ask the
    iShares site's search when the fund finder fails (only for iShares funds - a search per stock costs requests
    and gets the pass refused)."""
    sources.note, sources.regions = "", []
    if page and lgim_doc(page):
        return sources.document(page, "L&G"), "L&G"
    if by in FETCHERS and isin and not page and not product:
        if lines := getattr(sources, FETCHERS[by])(isin):
            return lines, by
        raise NoSource(isin)
    product = product or (ishares_page(page) if page else
                          sources.ishares_product(isin, search=search) if isin else None)
    sources.last = product
    if product:
        return sources.ishares(*product), "iShares"
    raise NoSource(isin)


# ── the depot ────────────────────────────────────────────────────────────────
@dataclass
class Depot:
    # {id, isin, name, currency, value, cost, parts: {account id: value}, trades: [costs.Trade]} per asset,
    # largest first
    positions: list[dict] = field(default_factory=list)
    cash: dict[str, Decimal] = field(default_factory=dict)  # per account name, in the base currency
    base: str = "EUR"

    @property
    def total(self) -> Decimal:
        return sum((p["value"] for p in self.positions), Decimal(0)) + sum(self.cash.values(), Decimal(0))

    @property
    def gain_share(self) -> Decimal | None:
        """Part of the securities' value that is gain (for the tax of a sale), from Wealthfolio's cost basis."""
        known = [p for p in self.positions if p.get("cost")]
        value = sum((p["value"] for p in known), Decimal(0))
        return max(Decimal(0), 1 - sum((p["cost"] for p in known), Decimal(0)) / value) if value else None


def _dec(value) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return Decimal(0)
    return number if number.is_finite() else Decimal(0)


def read_depot(wf, learned: dict[str, str] | None = None) -> Depot:
    """Every open position of every Wealthfolio account, added up per asset, values in the base currency.
    ``learned``: asset id -> ISIN the sync learned, for assets Wealthfolio keeps without one."""
    depot = Depot()
    isins = {a.get("id"): isin_of(a) or (learned or {}).get(a.get("id") or "", "") for a in wf.list_assets()}
    positions: dict[str, dict] = {}
    accounts = wf.list_accounts()
    for account in accounts:
        for h in wf.holdings(account.id):
            inst = h.get("instrument") or {}
            value = _dec((h.get("marketValue") or {}).get("base"))
            depot.base = h.get("baseCurrency") or depot.base
            kind = (h.get("holdingType") or "").lower()
            if kind == "cash":
                amount = value or _dec(h.get("quantity"))
                depot.cash[account.name] = depot.cash.get(account.name, Decimal(0)) + amount
            if kind != "security" or h.get("isClosed") or not inst.get("id"):
                continue
            isin = isins.get(inst["id"]) or isin_of({"instrumentSymbol": inst.get("symbol")}) or ""
            p = positions.setdefault(inst["id"], {"id": inst["id"], "isin": isin,
                                                  "name": inst.get("name") or inst.get("symbol") or inst["id"],
                                                  "currency": h.get("localCurrency") or "", "value": Decimal(0),
                                                  "cost": Decimal(0), "parts": {}, "trades": []})
            p["value"] += value
            p["parts"][account.id] = p["parts"].get(account.id, Decimal(0)) + value
            p["cost"] += _dec((h.get("costBasis") or {}).get("base"))
    depot.positions = sorted((p for p in positions.values() if p["value"] > 0), key=lambda p: -p["value"])
    if depot.positions:
        # shortcut: fees in the activity's currency, taken as the base currency; convert with fxRate if needed.
        for a in wf.search_activities([acc.id for acc in accounts], date(1970, 1, 1), date(2100, 1, 1)):
            held = positions.get(a.get("assetId") or "")
            if held is not None and a.get("activityType") in ("BUY", "SELL") and a.get("date"):
                held["trades"].append(costs.Trade(when(a["date"]).date(), a["activityType"],
                                               _dec(a.get("quantity")) * _dec(a.get("unitPrice")),
                                               _dec(a.get("fee")), _dec(a.get("tax")), a.get("accountId") or ""))
    return depot


# ── after a run ──────────────────────────────────────────────────────────────
BUDGET = 300  # seconds a pass may fetch; it runs under the sync lock, after the brokers


@dataclass
class Result:
    fetched: int = 0
    none: int = 0
    errors: list[str] = field(default_factory=list)


def status(state: State, asset_id: str) -> dict:
    """Where an asset's holdings come from: {status: ok|upload|none|error, source, url, at, error, lines}."""
    return json.loads(state.meta(meta_keys.FUND(asset_id)) or "{}")


def set_status(state: State, asset_id: str, **values) -> None:
    state.set_meta(meta_keys.FUND(asset_id), json.dumps({**status(state, asset_id), **values, "at": now()}))


def _days(at: str | None) -> float:
    return (datetime.now(UTC) - datetime.fromisoformat(at)).total_seconds() / 86400 if at else 1e9


# The issuers a fund's name in Wealthfolio names; those of FETCHERS are fetched by ISIN.
ISSUERS = {"iShares": r"\bishares\b", "SPDR": r"\bspdr\b|state street", "Xtrackers": r"x-?trackers",
           "Amundi": r"\bamundi\b|\blyxor\b", "Invesco": r"\binvesco\b", "Vanguard": r"\bvanguard\b",
           "L&G": r"\bl ?& ?g\b|legal ?& ?general", "VanEck": r"\bvan ?eck\b", "WisdomTree": r"wisdom ?tree",
           "HSBC": r"\bhsbc\b", "UBS": r"\bubs\b", "Franklin": r"\bfranklin\b",
           "J.P. Morgan": r"\bjpm\b|j\.? ?p\.? ?morgan", "Fidelity": r"\bfidelity\b", "Global X": r"\bglobal ?x\b",
           "Deka": r"\bdeka\b", "First Trust": r"first trust", "ARK": r"\bark\b|\brize\b",
           "BNP Paribas": r"\bbnp\b",
           "HANetf": r"\bhan-?etf\b|future of defence|\bemqq\b"}
FETCHERS = {"Xtrackers": "xtrackers", "SPDR": "spdr", "Amundi": "amundi", "Invesco": "invesco", "Vanguard": "vanguard",
            "VanEck": "vaneck", "ARK": "ark", "HANetf": "hanetf"}
FUND_NAME = re.compile(r"\b(?:etf|ucits|fonds|fund|index|msci|ftse|s&p ?500|stoxx|nasdaq)\b", re.I)


def issuer(position: dict, st: dict) -> str:
    """The fund's issuer, as far as we can tell: the source of its list, a saved iShares page, else its name."""
    if st.get("source") and st["source"] != "Upload":
        return st["source"]
    if st.get("url"):
        return "L&G" if lgim_doc(st["url"]) else "iShares"
    return (next((k for k, pattern in ISSUERS.items() if re.search(pattern, position["name"], re.I)), "")
            or next((k for k, ids in PRODUCTS.items() if position.get("isin") in ids), ""))


def is_fund(position: dict, st: dict) -> bool:
    """A fund rather than a stock: it has a list, or its name says so (ETF, UCITS, an index or an issuer)."""
    return bool(st.get("source") or st.get("url") or issuer(position, st) or FUND_NAME.search(position["name"]))


def is_ishares(position: dict, st: dict) -> bool:
    return issuer(position, st) == "iShares"


def update(state: State, depot: Depot, sources: Sources, *, force: bool = False, budget: float = BUDGET,
           only: str = "") -> Result:
    """Fetch the holdings of the depot's positions that are due; an uploaded file is kept unless the user
    saved an iShares page for the fund. ``force``: everything now (the button on *X-Ray*); ``only``: just
    this asset (its own button). Why an iShares fund got no list is stored at the fund, for the page."""
    deadline = time.monotonic() + budget
    try:
        return _update(state, depot, sources, force, deadline, only)
    finally:
        state.delete_meta(meta_keys.FUNDS_PROGRESS())


def _update(state: State, depot: Depot, sources: Sources, force: bool, deadline: float, only: str) -> Result:
    result = Result()
    todo = [p for p in depot.positions if p["id"] == only] if only else depot.positions
    for n, p in enumerate(todo, 1):
        if time.monotonic() > deadline:  # the rest is due again next run
            log.info("look-through: time budget used up, the remaining funds next run")
            break
        state.set_meta(meta_keys.FUNDS_PROGRESS(), json.dumps({"n": n, "m": len(todo), "name": p["name"]}))
        st = status(state, p["id"])
        if st.get("status") == "upload" and not st.get("url"):
            continue
        due = {"ok": REFRESH_DAYS, "none": NONE_DAYS, "error": REFRESH_DAYS}.get(st.get("status", ""), 0)
        if not force and _days(st.get("at")) < due:
            continue
        by = issuer(p, st)
        try:
            if not (p["isin"] or st.get("url")):
                if not is_fund(p, st):
                    continue
                hint = " Trag unter „Einstellungen dieses ETFs“ seine Produktseite bei iShares ein."
                raise SourceError("Wealthfolio kennt keine ISIN zu diesem ETF." + (hint if by == "iShares" else ""))
            known = tuple(st["product"]) if st.get("product") and not st.get("url") else None
            lines, source = fetch(sources, p["isin"], st.get("url", ""), product=known, search=by == "iShares", by=by)
        except NoSource:
            if is_fund(p, st):
                _failed(state, p, result, SourceError(_unknown(by, p["isin"])))
                continue
            set_status(state, p["id"], status="none", source="", error="")
            result.none += 1
        except SourceError as e:
            set_status(state, p["id"], status="error", error=str(e))
            if is_fund(p, st):  # a stock the search couldn't check isn't shown, so it isn't counted either
                result.errors.append(f"{p['name']}: {e}")
        else:
            state.set_fund(p["id"], p["name"], rows(lines))
            ter = sources.ter(p["isin"])
            set_status(state, p["id"], status="ok", source=source, error="", lines=len(lines), product=sources.last,
                       note=sources.note, regions=sources.regions or sources.fund_country(p["isin"]),
                       **({"ter": str(ter), "ter_source": source, "ter_at": now()} if ter is not None else {}))
            result.fetched += 1
    _fill_ters(state, depot, sources, deadline, only, force)
    record(state, fetched=result.fetched, none=result.none, failed=len(result.errors),
           finder=sources.finder_error)
    return result


def _fill_ters(state: State, depot: Depot, sources: Sources, deadline: float, only: str, force: bool) -> None:
    """Every fund's Finanzfluss page, every ``TER_DAYS``: its TER where the issuer lists none (the user's own stays
    first), and its countries and industries - the last resort for Wealthfolio's taxonomies (``_write_asset``)."""
    funds = state.funds()
    for p in depot.positions:
        st = status(state, p["id"])
        if (only and p["id"] != only) or not p["isin"] or not (p["id"] in funds or is_fund(p, st)):
            continue
        if not force and _days(st.get("ter_asked")) < TER_DAYS:
            continue  # asked lately (with or without an answer)
        if time.monotonic() > deadline:
            break
        try:
            ff = sources.finanzfluss(p["isin"])
        except SourceError as e:
            log.info("look-through: nothing from Finanzfluss for %s: %s", p["isin"], e)
            continue
        # An empty answer (Finanzfluss changed its page?) never replaces what was read before (#134 D1).
        found: dict = {k: ff[v] for k, v in (("ff_regions", "regions"), ("ff_sectors", "sectors")) if ff[v]}
        if not found and ff["ter"] is None:
            log.warning("look-through: Finanzfluss' page for %s gave nothing - has its layout changed?", p["isin"])
        if ff["ter"] is not None and not (st.get("ter") and st.get("ter_source") != "Finanzfluss"):
            found |= {"ter": str(ff["ter"]), "ter_source": "Finanzfluss", "ter_at": now()}
        set_status(state, p["id"], ter_asked=now(), **found)


def _unknown(by: str, isin: str) -> str:
    """Why a fund got no list: its issuer doesn't know the ISIN, or there is no source for its issuer yet."""
    if by == "iShares":
        return (f"iShares kennt die ISIN {isin} nicht (Fondsfinder und Suche) - trag unter „Einstellungen dieses "
                "ETFs“ seine Produktseite bei iShares ein.")
    if by in FETCHERS:
        return f"{by} kennt die ISIN {isin} nicht."
    if by == "L&G":
        return ("L&G: trag unter „Einstellungen dieses ETFs“ einmal die Adresse seiner Positionsliste ein "
                "(Fundholdings.csv) - danach holt der Dienst sie jeden Tag selbst.")
    upload = " Lade seine Positionsliste unter „Einstellungen dieses ETFs“ hoch."
    return (f"Für {by} gibt es keinen automatischen Abruf.{upload}" if by else
            f"Der Anbieter dieses ETFs ist unbekannt - kein automatischer Abruf.{upload}")


def _failed(state: State, p: dict, result: Result, e: SourceError) -> None:
    set_status(state, p["id"], status="error", error=str(e))
    result.errors.append(f"{p['name']}: {e}")


def record(state: State, **values) -> None:
    """The last pass, for the page's header: {at, fetched, none, failed, finder, error}."""
    state.set_meta(meta_keys.FUNDS_LAST(), json.dumps({**values, "at": now()}))


def last_pass(state: State) -> dict:
    return json.loads(state.meta(meta_keys.FUNDS_LAST()) or "{}")


def ter(st: dict) -> tuple[Decimal | None, str]:
    """A position's TER and where it is from: the user's own first, then the issuer's."""
    for key, source in (("ter_manual", "manual"), ("ter", st.get("ter_source", ""))):
        if st.get(key) not in (None, ""):
            return Decimal(st[key]), source
    return None, ""


def model(saved: dict[str, str]) -> costs.Model:
    return costs.Model(*(Decimal(saved[k]) if saved.get(k) else None for k in ("fixed", "percent", "spread",
                                                                                "custody")))


def cost(state: State, depot: Depot, cfg) -> costs.DepotCost:
    """The depot's costs from buy to sale, with the brokers' fee models (``Config.cost_models``)."""
    broker = {a: key for key, b in cfg.brokers.items()
              for a in (b.cash_account_id, b.portfolio_account_id, b.savings_account_id) if a}
    positions = [costs.Position(p["id"], p["name"], p["value"], p["cost"], p.get("parts", {}),
                                tuple(p.get("trades", ())), ter(status(state, p["id"]))[0]) for p in depot.positions]
    return costs.depot(positions, datetime.now(UTC).date(), broker,
                       {k: model(v) for k, v in cfg.cost_models.items()})


def rows(lines: tuple[Constituent, ...]) -> list[list[str]]:
    return [[c.key, c.name, str(c.weight), c.country, c.sector, c.currency] for c in lines]


def holdings(state: State, depot: Depot) -> list[xray.Holding]:
    """The depot for ``xray``: a position with a holdings list is a fund, any other one stock of its own."""
    funds = state.funds()
    out = []
    for p in depot.positions:
        f = funds.get(p["id"])
        if f:
            lines = tuple(Constituent(k, n, Decimal(w), co, se, cu) for k, n, w, co, se, cu in f["constituents"])
            out.append(xray.Holding(p["id"], p["name"], p["value"], lines))
        else:
            out.append(xray.stock(p["id"], p["isin"] or p["id"], p["name"], p["value"], currency=p["currency"]))
    return xray.unify(out)


def limits(saved: dict[str, str]) -> dict[str, Decimal]:
    out = dict(xray.LIMITS)
    for k, v in saved.items():
        if k in out:
            try:
                out[k] = Decimal(v)
            except InvalidOperation:
                continue
    return out


def accepted(state: State) -> set[str]:
    return set(json.loads(state.meta(meta_keys.XRAY_ACCEPTED()) or "[]"))


def new_risks(state: State, found: list[xray.Finding]) -> list[xray.Finding]:
    """The risks neither accepted nor notified before; remembers the current ones as notified (one that goes
    away and comes back is new again)."""
    current = [f for f in found if f.key not in accepted(state)]
    told = set(json.loads(state.meta(meta_keys.XRAY_NOTIFIED()) or "[]"))
    state.set_meta(meta_keys.XRAY_NOTIFIED(), json.dumps(sorted(f.key for f in current)))
    return [f for f in current if f.key not in told]


# ── into Wealthfolio's taxonomies ────────────────────────────────────────────
SOURCE = "BROKERSYNC"  # the assignments' source: ours; AUTO is Yahoo's, anything else the user's
# The issuers' sector names that differ from Wealthfolio's GICS sectors (all lower case), German ones included -
# iShares' German download and DWS name them in German.
ALIASES = {"communication": "communication services", "telecommunication services": "communication services",
           "telecommunications": "communication services", "it": "information technology",
           "informationstechnologie": "information technology", "finanzwesen": "financials",
           "finanzen": "financials", "gesundheitswesen": "health care", "gesundheitsversorgung": "health care",
           "gesundheit": "health care", "industrie": "industrials", "nicht-basiskonsumgüter": "consumer discretionary",
           "zyklische konsumgüter": "consumer discretionary", "basiskonsumgüter": "consumer staples",
           "nichtzyklische konsumgüter": "consumer staples", "kommunikation": "communication services",
           "kommunikationsdienste": "communication services", "energie": "energy", "materialien": "materials",
           "roh-, hilfs- & betriebsstoffe": "materials", "grundstoffe": "materials", "versorger": "utilities",
           "versorgungsbetriebe": "utilities", "immobilien": "real estate",
           # DWS (Xtrackers) names its sectors in German, ICB style
           "technologie": "information technology", "industrieunternehmen": "industrials",
           "verbrauchsgüter": "consumer discretionary", "finanzdienstleister": "financials",
           "telekommunikation": "communication services", "versorgungsunternehmen": "utilities",
           "material": "materials",
           # GICS 2023 names the issuers use, where Wealthfolio's taxonomy still has the earlier ones
           "airlines": "passenger airlines", "automobile components": "auto components",
           "ground transportation": "road & rail", "marine transportation": "marine",
           "personal care products": "personal products",
           # Finanzfluss' industries without a GICS sector of their own (#166): each to the GICS industry it
           # belongs to (rolled up to the sector) - waste management is Commercial Services & Supplies
           # (Industrials), education Diversified Consumer Services (Consumer Discretionary), agriculture
           # Agricultural Products & Services (Consumer Staples), medical diagnostics & research Life Sciences
           # Tools & Services (Health Care)
           "abfallwirtschaft": "environmental & facilities services", "bildung": "education services",
           "forsterzeugnisse": "paper & forest products",
           "kleidung & accessoires": "apparel accessories & luxury goods",
           "konglomerate": "industrial conglomerates", "landwirtschaft": "agricultural products",
           "med. diagnostik & forschung": "life sciences tools & services",
           "unternehmensdienste": "commercial & professional services"}
# Names that are no country or sector at all - cash, derivatives, "other", unknown, XE (no ISO code): never given
# a category, and listed apart from the names Wealthfolio merely lacks a category for (#166).
NO_CATEGORY = frozenset({"barmittel & derivate", "barmittel und/oder derivate", "cash und/oder derivate",
                         "cash and/or derivatives", "cash", "derivate", "derivatives", "other", "others", "sonstige",
                         "andere", "unbekannt", "unknown", "xe", "-"})
# A country as ISO 3166-1 alpha-2, as Wealthfolio keys its regions (``country_US``): the issuers' spellings that
# aren't Wealthfolio's English name - German ones (iShares' German download, DWS, VanEck) and English variants.
# A two-letter code (Vanguard) is taken as it is. Names Wealthfolio has, in English, need no entry.
COUNTRIES = {
    "usa": "us", "united states of america": "us", "vereinigte staaten": "us", "vereinigte staaten von amerika": "us",
    "uk": "gb", "great britain": "gb", "vereinigtes königreich": "gb", "großbritannien": "gb",
    "korea": "kr", "korea (south)": "kr", "korea, republic of": "kr", "south korea": "kr", "republic of korea": "kr",
    "südkorea": "kr", "korea (süd)": "kr", "republik korea": "kr", "netherlands (the)": "nl", "niederlande": "nl",
    "russian federation": "ru", "russland": "ru", "russische föderation": "ru", "taiwan, province of china": "tw",
    "taiwan (republik china)": "tw", "hong kong sar": "hk", "hongkong": "hk", "czech republic": "cz",
    "tschechien": "cz", "tschechische republik": "cz", "türkiye": "tr", "turkey": "tr", "türkei": "tr",
    "kanada": "ca", "frankreich": "fr", "deutschland": "de", "schweiz": "ch", "indien": "in", "brasilien": "br",
    "australien": "au", "spanien": "es", "italien": "it", "schweden": "se", "dänemark": "dk", "norwegen": "no",
    "finnland": "fi", "irland": "ie", "belgien": "be", "österreich": "at", "singapur": "sg", "neuseeland": "nz",
    "mexiko": "mx", "südafrika": "za", "saudi-arabien": "sa", "vereinigte arabische emirate": "ae", "katar": "qa",
    "indonesien": "id", "philippinen": "ph", "polen": "pl", "griechenland": "gr", "kolumbien": "co",
    "argentinien": "ar", "ungarn": "hu", "luxemburg": "lu", "kaimaninseln": "ky", "cayman-inseln": "ky",
    "insel man": "im", "ägypten": "eg", "zypern": "cy", "estland": "ee", "lettland": "lv", "litauen": "lt",
    "slowenien": "si", "slowakei": "sk", "rumänien": "ro", "kroatien": "hr", "marokko": "ma", "kenia": "ke",
    "kasachstan": "kz", "britische jungferninseln": "vg", "jordanien": "jo", "bangladesch": "bd", "island": "is",
    "china (volksrepublik)": "cn", "volksrepublik china": "cn", "macao": "mo", "makau": "mo",
    "großbritannien (uk)": "gb", "süd korea": "kr", "süd-korea": "kr", "taiwan region": "tw", "hong kong": "hk"}
# Taxonomy, the line's attribute, and which of its categories count (countries; every GICS level - an industry
# group or industry, as SPDR and Vanguard name them, counts for its sector).
TAXONOMIES = (("regions", "country", lambda c: str(c.get("id", "")).startswith("country_")),
              ("industries_gics", "sector", lambda c: True))


@dataclass
class Written:
    written: int = 0  # taxonomies of assets replaced
    manual: int = 0  # left alone: the user assigned them
    unmatched: list[str] = field(default_factory=list)  # names Wealthfolio has no category for
    deliberate: list[str] = field(default_factory=list)  # names that are no country or sector (NO_CATEGORY)


def _bp(lines: list[list[str]], attr: int, categories: dict[str, str], unmatched: set[str]) -> list[tuple[str, int]]:
    """Weight per category in basis points, rounded down; a name without a category stays out (unassigned)."""
    out: dict[str, Decimal] = {}
    aliases = {_norm(k): v for k, v in (COUNTRIES if attr == 3 else ALIASES).items()}
    for line in lines:
        name = _norm(line[attr])
        if not name:
            continue
        category = None if name in NO_CATEGORY else categories.get(aliases.get(name, name))
        if category is None:
            unmatched.add(line[attr].strip())
            continue
        out[category] = out.get(category, Decimal(0)) + Decimal(line[2])
    # Negative cash or derivative lines can push the rest above 100 %; Wealthfolio refuses more than 10000 bp.
    total = sum(out.values(), Decimal(0))
    scale = 1 / total if total > 1 else Decimal(1)
    return sorted((c, int(w * scale * 10000)) for c, w in out.items() if int(w * scale * 10000) >= 1)


def _names(found: list[dict], counts) -> dict[str, str]:
    """Category name (lower case) -> the id to assign: a GICS subcategory rolls up to its sector."""
    parent: dict[str, str] = {c["id"]: c["parentId"] for c in found if c.get("parentId")}

    def top(cid: str) -> str:
        seen: set[str] = set()
        while cid in parent and cid not in seen:
            seen.add(cid)
            cid = parent[cid]
        return cid
    return {_norm(str(c.get("name", ""))): c["id"] if str(c["id"]).startswith("country_") else top(c["id"])
            for c in found if counts(c)}


def _norm(name: str) -> str:
    """A category name as compared: lower case, without commas (SPDR: "Oil Gas & Consumable Fuels")."""
    return " ".join(name.replace(",", " ").lower().split())


def write_taxonomies(state: State, wf, depot: Depot) -> Written:
    """Countries and sectors of every fund with a holdings list into Wealthfolio, so its own allocation views
    see through the funds. Only Yahoo's (``AUTO``) and our own assignments are replaced; unchanged ones are
    not written again."""
    result, unmatched = Written(), set[str]()
    categories = {tax: _names(wf.taxonomy_categories(tax), counts) for tax, _, counts in TAXONOMIES}
    categories["regions"].update({c[len("country_"):].lower(): c for c in list(categories["regions"].values())})
    funds = state.funds()
    # A line without a country takes the one another issuer names for the same company (iShares names none).
    known = {xray.plain(line[1]): line[3] for f in funds.values() for line in f["constituents"] if line[3]}
    for p in depot.positions:
        fund, st = funds.get(p["id"]), status(state, p["id"])
        fallback = [["", "", w, code, "", ""] for code, w in st.get("ff_regions") or []] + [
            ["", "", w, "", sector, ""] for sector, w in st.get("ff_sectors") or []]
        if not fund and not fallback:
            continue
        try:
            used = _write_asset(wf, p["id"], _lines(fund["constituents"], st, known) if fund else [], categories,
                                unmatched, result, fallback)
            if used != st.get("from_finanzfluss", []):
                set_status(state, p["id"], from_finanzfluss=used)  # shown at the fund's card
        except WealthfolioError:  # one fund Wealthfolio refuses never keeps the others out
            log.exception("look-through: countries and sectors of %s not written", p["id"])
    result.deliberate = sorted(n for n in unmatched if _norm(n) in NO_CATEGORY)[:20]
    result.unmatched = sorted(n for n in unmatched if _norm(n) not in NO_CATEGORY)[:20]
    state.set_meta(meta_keys.TAXONOMIES_LAST(), json.dumps({"written": result.written, "manual": result.manual,
                                                            "unmatched": result.unmatched,
                                                            "deliberate": result.deliberate, "at": now()}))
    return result


def _lines(constituents: list[list[str]], st: dict, known: dict[str, str]) -> list[list[str]]:
    """A fund's lines for its taxonomies: the issuer's own breakdown of its countries first (iShares), else each
    line's country, else the one another fund's issuer names for that company."""
    if breakdown := st.get("regions"):
        return [[*line[:3], "", *line[4:]] for line in constituents] + [["", "", w, code, "", ""]
                                                                         for code, w in breakdown]
    return [[*line[:3], line[3] or known.get(xray.plain(line[1]), ""), *line[4:]] for line in constituents]


def _write_asset(wf, asset_id: str, constituents: list[list[str]], categories: dict[str, dict[str, str]],
                 unmatched: set[str], result: Written, fallback: list[list[str]] | None = None) -> list[str]:
    """Write the asset's taxonomies; returns those that took Finanzfluss' breakdown."""
    existing, used = wf.asset_assignments(asset_id), list[str]()
    for tax, attr, _ in TAXONOMIES:
        column = 3 if attr == "country" else 4
        new = _bp(constituents, column, categories[tax], unmatched)
        alt = _bp(fallback or [], column, categories[tax], unmatched)  # Finanzfluss' breakdown of the whole fund
        if sum(w for _, w in new) < FALLBACK_BELOW and sum(w for _, w in alt) > sum(w for _, w in new):
            new, used = alt, [*used, tax]  # the issuer's list names too little: the last resort
        current = [a for a in existing if a.get("taxonomyId") == tax]
        if any(str(a.get("source", "")).upper() not in ("AUTO", SOURCE) for a in current):
            result.manual += 1
            continue
        if not new:  # the list no longer names countries or sectors: ours go, Yahoo's stay
            for a in current:
                if a.get("source") == SOURCE:
                    wf.delete_assignment(a["id"])
            continue
        if sorted((a.get("categoryId"), a.get("weight")) for a in current) == new and all(
                a.get("source") == SOURCE for a in current):
            continue
        wf.replace_assignments(asset_id, tax, [{"assetId": asset_id, "taxonomyId": tax, "categoryId": c,
                                                "weight": w, "source": SOURCE} for c, w in new])
        result.written += 1
    return used


# ── asset classes (waldonso2/wealthfolio-importer-addon#141) ────────────────
ASSET_CLASSES = "asset_classes"
# Wealthfolio's own fund-name rules (auto_classification.rs: is_bond_fund_name, is_physical_precious_metal_name),
# upper case: what it would assign itself if it found the fund at a quote provider. Nothing else is inferred - a
# fund's name without these words gets no class (an equity fund is never assumed).
BOND_WORDS = (" BOND", "BOND ", "BONDS", "FIXED INCOME", "TREASURY", "T-BILL", "T-NOTE", "T-BOND", " GILT", "GILTS",
              "GOVERNMENT BOND", "GOVT BOND", "SOVEREIGN", "BUNDESREPUBLIK", "BUNDESOBLIGATION")
METALS = ("GOLD", "SILVER", "PLATINUM", "PALLADIUM")
PHYSICAL = ("PHYSICAL", "BULLION", " ETC", " ETC/", " ETP")


def name_class(name: str) -> str | None:
    """The asset class a fund's name names: FIXED_INCOME, COMMODITIES (physical precious metal) or None."""
    n = name.upper()
    if any(w in n for w in BOND_WORDS):
        return "FIXED_INCOME"
    return "COMMODITIES" if any(w in n for w in METALS) and any(w in n for w in PHYSICAL) else None


def asset_classes(state: State, wf, depot: Depot, write: bool) -> dict:
    """Positions Wealthfolio has no asset class for (its allocation counts them as "Unknown"), kept for *Prüfung*
    and the service's page. With ``write`` a fund whose name names its class (``name_class``) gets it with 100 %
    and ``source: AUTO``, so Wealthfolio replaces it with a quote provider's real breakdown; an asset with any
    asset class - the user's above all - is never touched."""
    known = {str(c.get("id")) for c in wf.taxonomy_categories(ASSET_CLASSES)} if write else set()
    before = json.loads(state.meta(meta_keys.ASSET_CLASSES()) or "{}").get("named", {})
    missing, named = [], {}
    for p in depot.positions:
        current = [a for a in wf.asset_assignments(p["id"]) if a.get("taxonomyId") == ASSET_CLASSES]
        if current:
            if p["id"] in before and [(a.get("categoryId"), a.get("source")) for a in current] == [
                    (before[p["id"]]["class"], "AUTO")]:
                named[p["id"]] = before[p["id"]]  # still ours from the name: keep showing where it came from
            continue
        cls = name_class(p["name"]) if is_fund(p, status(state, p["id"])) else None
        if write and cls in known:
            wf.replace_assignments(p["id"], ASSET_CLASSES, [{"assetId": p["id"], "taxonomyId": ASSET_CLASSES,
                                                              "categoryId": cls, "weight": 10000, "source": "AUTO"}])
            named[p["id"]] = {"name": p["name"], "class": cls}
            continue
        missing.append({"id": p["id"], "name": p["name"], "isin": p["isin"]})
    found = {"missing": missing, "named": named, "at": now()}
    state.set_meta(meta_keys.ASSET_CLASSES(), json.dumps(found))
    return found


def remove_taxonomies(wf) -> int:
    """Take our assignments out of Wealthfolio again (Yahoo fills its own back in), also of assets sold
    since; returns how many."""
    removed = 0
    for asset in wf.list_assets():
        for a in wf.asset_assignments(asset["id"]):
            if a.get("source") == SOURCE:
                wf.delete_assignment(a["id"])
                removed += 1
    return removed
