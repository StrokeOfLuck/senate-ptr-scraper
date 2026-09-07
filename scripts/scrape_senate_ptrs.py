"""Automated scraper for official electronic Senate Periodic Transaction Reports (PTRs).

This is the automation-friendly counterpart to
notebooks/Scrape_Official_Senate_PTRs_Electronic_Only.ipynb. The notebook
remains available for manual/exploratory runs in Colab (with Google Drive
storage); this script is what the scheduled GitHub Action runs, writing
data to a local `data/` folder inside this repo instead of Google Drive.

Paper (scanned) PTRs are intentionally out of scope here, same as the
notebook -- they're recorded in the filing index and status file but never
downloaded or parsed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = REPO_ROOT / "data"
RAW_DIR = DATA_DIR / "01_html"
INDEX_DIR = DATA_DIR / "02_filing_index"
TRANSACTIONS_DIR = DATA_DIR / "03_transactions"
STATUS_DIR = DATA_DIR / "04_status"

for folder in [DATA_DIR, RAW_DIR, INDEX_DIR, TRANSACTIONS_DIR, STATUS_DIR]:
    folder.mkdir(parents=True, exist_ok=True)

FILING_INDEX_CSV = INDEX_DIR / "senate_ptr_filing_index.csv"
TRANSACTIONS_CSV = TRANSACTIONS_DIR / "senate_ptr_transactions_electronic.csv"
STATUS_CSV = STATUS_DIR / "senate_ptr_scrape_status.csv"
METADATA_JSON = DATA_DIR / "senate_ptr_metadata.json"

# Overridable via environment variables so the GitHub Action can tune runs
# (e.g. a full backfill vs. a routine incremental run) without editing code.
# Use `or default` rather than `.get(key, default)` -- GitHub Actions sets
# these env vars to an empty string (not unset) both for scheduled runs
# (no workflow_dispatch inputs exist) and manual runs with blank inputs,
# and `.get()` only falls back to its default when the key is missing
# entirely, not when it's present-but-empty. This was the root cause of
# every automated run crashing instantly on pd.to_datetime("").
START_DATE = os.environ.get("SENATE_PTR_START_DATE") or "2021-01-01"
END_DATE = os.environ.get("SENATE_PTR_END_DATE") or date.today().isoformat()

BATCH_SIZE = int(os.environ.get("SENATE_PTR_BATCH_SIZE", "100"))
REQUEST_DELAY = float(os.environ.get("SENATE_PTR_REQUEST_DELAY", "0.40"))
MAX_SEARCH_PAGES = int(os.environ.get("SENATE_PTR_MAX_SEARCH_PAGES", "500"))

# Save the official HTML page for each electronic PTR.
SAVE_ELECTRONIC_HTML = os.environ.get("SENATE_PTR_SAVE_HTML", "true").lower() != "false"

# Reuse already-parsed electronic reports when a prior CSV exists.
RESUME_EXISTING = os.environ.get("SENATE_PTR_RESUME_EXISTING", "true").lower() != "false"

ROOT = "https://efdsearch.senate.gov"
LANDING_URL = f"{ROOT}/search/home/"
SEARCH_URL = f"{ROOT}/search/"
REPORTS_URL = f"{ROOT}/search/report/data/"

EXPECTED_HEADERS = [
    "#",
    "Transaction Date",
    "Owner",
    "Ticker",
    "Asset Name",
    "Asset Type",
    "Type",
    "Amount",
    "Comment",
]


# ---------------------------------------------------------------------------
# Session / auth
# ---------------------------------------------------------------------------

def build_session() -> requests.Session:
    client = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry)
    client.mount("https://", adapter)

    client.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    return client


def get_page_csrf(html: str):
    soup = BeautifulSoup(html, "lxml")
    token = soup.find(attrs={"name": "csrfmiddlewaretoken"})
    return token.get("value") if token else None


def accept_senate_agreement(client: requests.Session) -> str:
    r = client.get(LANDING_URL, timeout=30)

    if r.status_code == 403:
        raise RuntimeError(
            "Senate eFD returned HTTP 403 before the scrape began. "
            "This usually means the current runner IP is being blocked."
        )

    r.raise_for_status()

    form_csrf = get_page_csrf(r.text)
    if not form_csrf:
        raise RuntimeError("Could not locate the Senate agreement CSRF token.")

    accepted = client.post(
        LANDING_URL,
        data={
            "csrfmiddlewaretoken": form_csrf,
            "prohibition_agreement": "1",
        },
        headers={"Referer": LANDING_URL},
        timeout=30,
        allow_redirects=True,
    )

    if accepted.status_code == 403:
        raise RuntimeError("Senate eFD returned HTTP 403 while accepting the agreement.")

    accepted.raise_for_status()

    csrf = client.cookies.get("csrftoken") or client.cookies.get("csrf")

    if not csrf:
        raise RuntimeError("The Senate agreement was accepted, but no CSRF cookie was returned.")

    return csrf


# ---------------------------------------------------------------------------
# Filing index search
# ---------------------------------------------------------------------------

def mmddyyyy(iso_date: str) -> str:
    return pd.to_datetime(iso_date).strftime("%m/%d/%Y")


def search_ptr_batch(client: requests.Session, csrf: str, start: int = 0, length: int = 100) -> dict:
    payload = {
        "start": str(start),
        "length": str(length),
        "report_types": "[11]",
        "filer_types": "[1]",
        "submitted_start_date": f"{mmddyyyy(START_DATE)} 00:00:00",
        "submitted_end_date": f"{mmddyyyy(END_DATE)} 23:59:59",
        "csrfmiddlewaretoken": csrf,
    }

    r = client.post(
        REPORTS_URL,
        data=payload,
        headers={
            "Referer": SEARCH_URL,
            "X-CSRFToken": csrf,
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=60,
    )

    if r.status_code == 403:
        raise RuntimeError("Senate report search returned HTTP 403.")

    r.raise_for_status()

    try:
        return r.json()
    except Exception as exc:
        preview = r.text[:500].replace("\n", " ")
        raise RuntimeError(f"Senate search did not return JSON. Response started with: {preview}") from exc


def parse_search_result(row) -> dict:
    raw = list(row)

    first_name = str(raw[0]).strip() if len(raw) > 0 else ""
    last_name = str(raw[1]).strip() if len(raw) > 1 else ""
    office = str(raw[2]).strip() if len(raw) > 2 else ""
    link_html = str(raw[3]) if len(raw) > 3 else ""
    filing_date = str(raw[4]).strip() if len(raw) > 4 else ""

    soup = BeautifulSoup(link_html, "lxml")
    anchor = soup.find("a")

    relative_url = anchor.get("href") if anchor else ""
    report_url = urljoin(ROOT, relative_url) if relative_url else ""
    report_title = anchor.get_text(" ", strip=True) if anchor else ""

    # Keep paper filings in the index, but do not process them here.
    match = re.search(r"/search/view/(ptr|paper)/([0-9A-Za-z-]+)/?", report_url, flags=re.I)

    report_kind = match.group(1).lower() if match else ""
    report_id = match.group(2) if match else ""

    if report_kind == "ptr":
        filing_format = "electronic_html"
    elif report_kind == "paper":
        filing_format = "paper_scan"
    else:
        filing_format = "unknown"

    report_key = report_id or hashlib.sha256(report_url.encode("utf-8")).hexdigest()

    return {
        "first_name": first_name,
        "last_name": last_name,
        "filer_name": " ".join(x for x in [first_name, last_name] if x),
        "office": office,
        "filing_date": filing_date,
        "report_title": report_title,
        "report_id": report_id,
        "report_key": report_key,
        "filing_format": filing_format,
        "report_url": report_url,
    }


def build_filing_index(session: requests.Session, csrf_token: str) -> pd.DataFrame:
    filings = []
    offset = 0
    page = 0

    while True:
        page += 1

        if page > MAX_SEARCH_PAGES:
            raise RuntimeError(
                f"Exceeded MAX_SEARCH_PAGES ({MAX_SEARCH_PAGES}) while paginating "
                "the Senate PTR search. Aborting to avoid an unbounded loop -- "
                "check the API response shape, or raise this cap deliberately."
            )

        result = search_ptr_batch(session, csrf_token, start=offset, length=BATCH_SIZE)

        rows = result.get("data", [])
        if not rows:
            break

        filings.extend(parse_search_result(row) for row in rows)

        filtered_total = result.get("recordsFiltered")
        offset += len(rows)

        if filtered_total is not None:
            print(f"Found {len(filings)} / {filtered_total} filings")
        else:
            print(f"Found {len(filings)} filings")

        if len(rows) < BATCH_SIZE:
            break

        if filtered_total is not None and offset >= int(filtered_total):
            break

        time.sleep(REQUEST_DELAY)

    filings_df = pd.DataFrame(filings)

    if not filings_df.empty:
        filings_df["filing_date"] = pd.to_datetime(filings_df["filing_date"], errors="coerce").dt.strftime(
            "%Y-%m-%d"
        )

        filings_df = filings_df.drop_duplicates(subset=["report_key"], keep="first").reset_index(drop=True)

    filings_df.to_csv(FILING_INDEX_CSV, index=False)

    print()
    print(filings_df["filing_format"].value_counts(dropna=False) if not filings_df.empty else "No filings found")
    print("Total filing rows:", len(filings_df))
    print("Saved:", FILING_INDEX_CSV)

    return filings_df


# ---------------------------------------------------------------------------
# Electronic PTR parsing
# ---------------------------------------------------------------------------

def clean_text(value):
    if value is None:
        return None

    text = re.sub(r"\s+", " ", str(value)).strip()

    if text in {"", "--", "\u2014", "\u2013", "None", "null"}:
        return None

    return text


def normalize_header(value) -> str:
    text = clean_text(value) or ""
    text = text.replace("Transac- tion", "Transaction")
    text = text.replace("TransactionDate", "Transaction Date")
    return re.sub(r"\s+", " ", text).strip()


def parse_money_range(amount_text):
    text = clean_text(amount_text)
    if not text:
        return None, None

    nums = [int(x.replace(",", "")) for x in re.findall(r"\$\s*([0-9][0-9,]*)", text)]

    if not nums:
        return None, None

    lower = text.lower()

    if "over" in lower or "more than" in lower:
        return nums[0], None

    if len(nums) >= 2:
        return nums[0], nums[1]

    return nums[0], nums[0]


def iso_date_or_original(value):
    value = clean_text(value)
    if not value:
        return None

    parsed = pd.to_datetime(value, errors="coerce")

    if pd.isna(parsed):
        return value

    return parsed.strftime("%Y-%m-%d")


def now_utc() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def find_transaction_table(soup):
    for table in soup.find_all("table"):
        headers = [normalize_header(th.get_text(" ", strip=True)) for th in table.find_all("th")]

        joined = " | ".join(headers).lower()

        if "transaction" in joined and "owner" in joined and "ticker" in joined and "amount" in joined:
            return table

    for table in soup.find_all("table"):
        tbody = table.find("tbody")
        if tbody and tbody.find("tr"):
            if len(tbody.find("tr").find_all("td")) >= 8:
                return table

    return None


def extract_headers(table):
    first_row = table.find("tr")

    if first_row:
        headers = [normalize_header(x.get_text(" ", strip=True)) for x in first_row.find_all(["th", "td"])]

        if len(headers) >= 7:
            return headers

    return EXPECTED_HEADERS


def canonicalize_row(row_dict):
    lookup = {}

    for key, value in row_dict.items():
        norm = re.sub(r"[^a-z0-9]+", "", str(key).lower())
        lookup[norm] = clean_text(value)

    return {
        "item_number": lookup.get("") or lookup.get("number") or lookup.get("item"),
        "transaction_date": lookup.get("transactiondate") or lookup.get("date"),
        "owner": lookup.get("owner"),
        "ticker": lookup.get("ticker"),
        "asset_name": lookup.get("assetname"),
        "asset_type": lookup.get("assettype"),
        "transaction_type": lookup.get("type") or lookup.get("transactiontype"),
        "amount": lookup.get("amount"),
        "comment": lookup.get("comment"),
    }


def parse_electronic_ptr(html: str, filing: dict):
    soup = BeautifulSoup(html, "lxml")
    table = find_transaction_table(soup)

    if table is None:
        return [], "No electronic transaction table found"

    headers = extract_headers(table)
    body_rows = table.find("tbody").find_all("tr") if table.find("tbody") else table.find_all("tr")[1:]

    output = []

    for row_position, tr in enumerate(body_rows, start=1):
        values = [clean_text(td.get_text(" ", strip=True)) for td in tr.find_all("td")]

        if not values:
            continue

        use_headers = headers

        if len(values) == 9 and len(headers) != 9:
            use_headers = EXPECTED_HEADERS

        if len(use_headers) < len(values):
            use_headers = use_headers + [f"extra_{n}" for n in range(len(use_headers), len(values))]

        tx = canonicalize_row(dict(zip(use_headers[: len(values)], values)))

        transaction_date = iso_date_or_original(tx["transaction_date"])
        amount_min, amount_max = parse_money_range(tx["amount"])
        item_number = tx.get("item_number") or str(row_position)

        stable = "|".join(
            [
                filing.get("report_id") or filing.get("report_key") or "",
                str(item_number),
                transaction_date or "",
                tx.get("owner") or "",
                tx.get("ticker") or "",
                tx.get("asset_name") or "",
                tx.get("transaction_type") or "",
                tx.get("amount") or "",
                tx.get("comment") or "",
            ]
        )

        transaction_id = hashlib.sha256(stable.encode("utf-8")).hexdigest()

        output.append(
            {
                "transaction_id": transaction_id,
                "chamber": "Senate",
                "first_name": filing.get("first_name"),
                "last_name": filing.get("last_name"),
                "filer_name": filing.get("filer_name"),
                "office": filing.get("office"),
                "filing_date": filing.get("filing_date"),
                "report_title": filing.get("report_title"),
                "report_id": filing.get("report_id"),
                "report_key": filing.get("report_key"),
                "report_url": filing.get("report_url"),
                "item_number": item_number,
                "transaction_date": transaction_date,
                "owner": tx.get("owner"),
                "ticker": tx.get("ticker"),
                "asset_name": tx.get("asset_name"),
                "asset_type": tx.get("asset_type"),
                "transaction_type": tx.get("transaction_type"),
                "amount": tx.get("amount"),
                "amount_min": amount_min,
                "amount_max": amount_max,
                "comment": tx.get("comment"),
                "source_format": "electronic_html",
                "extraction_method": "official_html_table",
                "verification_status": "source_structured",
                "scraped_at_utc": now_utc(),
            }
        )

    return output, None


# ---------------------------------------------------------------------------
# Scraping electronic PTRs
# ---------------------------------------------------------------------------

def scrape_electronic_filings(session: requests.Session, filings_df: pd.DataFrame):
    existing_df = pd.DataFrame()

    if RESUME_EXISTING and TRANSACTIONS_CSV.exists():
        existing_df = pd.read_csv(TRANSACTIONS_CSV, low_memory=False)
        print("Resuming from:", TRANSACTIONS_CSV)
        print("Existing electronic transactions:", len(existing_df))

    completed_report_ids = (
        set(existing_df["report_id"].dropna().astype(str)) if not existing_df.empty else set()
    )

    electronic_filings = filings_df[filings_df["filing_format"] == "electronic_html"].copy()

    to_scrape = electronic_filings[
        ~electronic_filings["report_id"].astype(str).isin(completed_report_ids)
    ].reset_index(drop=True)

    print("Electronic PTR filings in index:", len(electronic_filings))
    print("Electronic PTR filings left to scrape:", len(to_scrape))

    new_rows = []
    electronic_status = []

    for i, filing_row in to_scrape.iterrows():
        filing = filing_row.to_dict()

        try:
            r = session.get(filing["report_url"], headers={"Referer": SEARCH_URL}, timeout=60)

            if r.status_code == 403:
                raise RuntimeError("HTTP 403")

            r.raise_for_status()

            if SAVE_ELECTRONIC_HTML:
                html_path = RAW_DIR / f"{filing['report_id']}.html"
                html_path.write_text(r.text, encoding="utf-8")

            rows, error = parse_electronic_ptr(r.text, filing)
            new_rows.extend(rows)

            electronic_status.append(
                {
                    "report_id": filing["report_id"],
                    "status": "ok" if error is None else "needs_review",
                    "transactions_found": len(rows),
                    "error": error or "",
                }
            )

            print(f"[{i + 1}/{len(to_scrape)}] {filing['filer_name']} | {len(rows)} transactions")

        except Exception as exc:
            electronic_status.append(
                {
                    "report_id": filing["report_id"],
                    "status": "error",
                    "transactions_found": 0,
                    "error": repr(exc),
                }
            )

            print(f"[{i + 1}/{len(to_scrape)}] ERROR | {filing['filer_name']} | {repr(exc)}")

        time.sleep(REQUEST_DELAY)

    frames = []

    if not existing_df.empty:
        frames.append(existing_df)

    if new_rows:
        frames.append(pd.DataFrame(new_rows))

    if frames:
        transactions_df = pd.concat(frames, ignore_index=True, sort=False)
        transactions_df = transactions_df.drop_duplicates(subset=["transaction_id"], keep="last").reset_index(
            drop=True
        )
    else:
        transactions_df = pd.DataFrame()

    transactions_df.to_csv(TRANSACTIONS_CSV, index=False)

    print()
    print("Electronic transactions available:", len(transactions_df))
    print("Saved:", TRANSACTIONS_CSV)

    return transactions_df, electronic_status


def build_status_table(filings_df: pd.DataFrame, transactions_df: pd.DataFrame, electronic_status: list) -> pd.DataFrame:
    new_status_map = {row["report_id"]: row for row in electronic_status}

    transaction_count_map = (
        transactions_df.groupby("report_id").size().to_dict() if not transactions_df.empty else {}
    )

    status_rows = []

    for _, filing_row in filings_df.iterrows():
        filing = filing_row.to_dict()

        report_id = filing["report_id"]
        filing_format = filing["filing_format"]

        if filing_format == "paper_scan":
            status = "paper_deferred"
            transactions_found = 0
            error = "Paper filing intentionally deferred to a future project."

        elif filing_format == "electronic_html":
            current = new_status_map.get(report_id)

            if current:
                status = current["status"]
                transactions_found = current["transactions_found"]
                error = current["error"]
            elif report_id in transaction_count_map:
                status = "ok_reused"
                transactions_found = int(transaction_count_map[report_id])
                error = ""
            else:
                status = "electronic_missing"
                transactions_found = 0
                error = "Electronic filing found in index but no parsed transactions are available."

        else:
            status = "unknown_format"
            transactions_found = 0
            error = "Unrecognized Senate report URL format."

        status_rows.append(
            {
                "report_id": report_id,
                "report_key": filing["report_key"],
                "filer_name": filing.get("filer_name"),
                "filing_date": filing.get("filing_date"),
                "filing_format": filing_format,
                "report_url": filing.get("report_url"),
                "status": status,
                "transactions_found": transactions_found,
                "error": error,
                "checked_at_utc": now_utc(),
            }
        )

    status_df = pd.DataFrame(status_rows)

    assert len(status_df) == len(filings_df)
    assert status_df["report_key"].nunique() == len(filings_df)

    status_df.to_csv(STATUS_CSV, index=False)

    print(status_df["status"].value_counts(dropna=False))
    print("Saved:", STATUS_CSV)

    return status_df


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    print("Filing range:", START_DATE, "through", END_DATE)

    session = build_session()
    csrf_token = accept_senate_agreement(session)
    print("Senate eFD session established successfully.")

    filings_df = build_filing_index(session, csrf_token)
    transactions_df, electronic_status = scrape_electronic_filings(session, filings_df)
    status_df = build_status_table(filings_df, transactions_df, electronic_status)

    electronic_filings_count = int((filings_df["filing_format"] == "electronic_html").sum())
    paper_filings_count = int((filings_df["filing_format"] == "paper_scan").sum())
    unknown_filings_count = int(
        (~filings_df["filing_format"].isin(["electronic_html", "paper_scan"])).sum()
    )

    latest_filing_date = None
    if not filings_df.empty and "filing_date" in filings_df.columns:
        parsed_filing_dates = pd.to_datetime(filings_df["filing_date"], errors="coerce")
        if parsed_filing_dates.notna().any():
            latest_filing_date = parsed_filing_dates.max().strftime("%Y-%m-%d")

    latest_transaction_date = None
    if not transactions_df.empty and "transaction_date" in transactions_df.columns:
        parsed_transaction_dates = pd.to_datetime(
            transactions_df["transaction_date"],
            errors="coerce",
        )
        if parsed_transaction_dates.notna().any():
            latest_transaction_date = parsed_transaction_dates.max().strftime("%Y-%m-%d")

    status_counts = {
        str(key): int(value)
        for key, value in status_df["status"].value_counts(dropna=False).to_dict().items()
    }

    metadata = {
        "updated_at_utc": now_utc(),
        "source": "U.S. Senate Electronic Financial Disclosure",
        "start_date": START_DATE,
        "end_date": END_DATE,
        "filings_total": int(len(filings_df)),
        "electronic_filings": electronic_filings_count,
        "paper_filings_deferred": paper_filings_count,
        "unknown_format_filings": unknown_filings_count,
        "electronic_transactions": int(len(transactions_df)),
        "latest_filing_date": latest_filing_date,
        "latest_transaction_date": latest_transaction_date,
        "status_counts": status_counts,
        "filing_index_csv": "data/02_filing_index/senate_ptr_filing_index.csv",
        "transactions_csv": "data/03_transactions/senate_ptr_transactions_electronic.csv",
        "status_csv": "data/04_status/senate_ptr_scrape_status.csv",
    }

    METADATA_JSON.write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )

    print("Saved metadata:", METADATA_JSON)
    print()
    print("SENATE PTR ELECTRONIC SCRAPE COMPLETE")
    print("====================================")
    print("All PTR filings indexed:", len(filings_df))
    print("Electronic filings:", electronic_filings_count)
    print("Paper filings deferred:", paper_filings_count)
    print("Electronic transactions:", len(transactions_df))


if __name__ == "__main__":
    main()
