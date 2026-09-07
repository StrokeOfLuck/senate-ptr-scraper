# Senate PTR Scraper

Collects official U.S. Senate Periodic Transaction Reports (PTRs) from the Senate Electronic Financial Disclosure system and converts electronic filings into structured transaction data.

Unlike House PTRs, many Senate filings are available as structured HTML. This repository focuses on those electronic filings while keeping paper filings visible in the filing index for separate processing.

## Official source

Senate PTR data is retrieved from the U.S. Senate Electronic Financial Disclosure system:

https://efdsearch.senate.gov/

The scraper accepts the site's disclosure agreement, searches the official PTR index for the configured date range, and follows each electronic filing to its transaction table.

## What the scraper does

1. Searches the official Senate eFD PTR index for a configurable date range.
2. Identifies electronic and paper filings.
3. Parses transaction tables from electronic PTRs.
4. Saves the official filing HTML for archival and verification.
5. Maintains filing-index, transaction, and scrape-status CSVs.
6. Reuses previously parsed electronic filings on later runs.

Paper filings are recorded but intentionally not parsed by this pipeline. They are marked `paper_deferred` for separate document-processing work.

## Repository data

Automated runs write into `data/`:

```text
data/
├── 01_html/           archived HTML for electronic filings
├── 02_filing_index/   senate_ptr_filing_index.csv
├── 03_transactions/   senate_ptr_transactions_electronic.csv
├── 04_status/         senate_ptr_scrape_status.csv
└── senate_ptr_metadata.json
```

The transaction CSV is the main structured output for downstream analysis and the portfolio site. `senate_ptr_metadata.json` summarizes update time, filing counts, transaction counts, latest dates, and scrape-status totals.

## Automation

The repository includes a GitHub Actions workflow at `.github/workflows/scrape.yml`.

It runs daily at approximately **9:30 AM U.S. Eastern**, installs the Python requirements, runs the scraper, commits updated data back to the repository, and can notify the `sean-data-portfolio` repository to rebuild when new Senate data is available.

The workflow also supports manual date-range overrides.

## Run locally

Create a virtual environment and install the requirements:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Run the scraper:

```powershell
python scripts/scrape_senate_ptrs.py
```

## Configuration

Runtime settings can be changed with environment variables:

```text
SENATE_PTR_START_DATE
SENATE_PTR_END_DATE
SENATE_PTR_BATCH_SIZE
SENATE_PTR_REQUEST_DELAY
SENATE_PTR_MAX_SEARCH_PAGES
SENATE_PTR_SAVE_HTML
SENATE_PTR_RESUME_EXISTING
```

The default date range begins on `2021-01-01` and ends on the current date.

Example:

```powershell
$env:SENATE_PTR_START_DATE="2025-01-01"
$env:SENATE_PTR_REQUEST_DELAY="0.75"
python scripts/scrape_senate_ptrs.py
```

## Electronic vs. paper filings

This distinction is important:

**Electronic PTRs** are parsed from the official Senate HTML transaction table.

**Paper PTRs** remain in the filing index but are not downloaded or extracted here.

Keeping those records in the index makes the gap explicit instead of silently excluding filings the current parser cannot handle.

## Reliability and source preservation

The scraper keeps the official HTML for electronic filings so parsed records can be checked against the source.

Requests include retry handling and a configurable delay between calls. The Senate eFD site can still return HTTP 403 responses to automated runners, so failed runs may require a later retry or a longer request delay.

A hard pagination limit is also used to prevent runaway searches if the eFD response format changes unexpectedly.

## Original notebook

The exploratory Colab version remains available at:

`notebooks/Scrape_Official_Senate_PTRs_Electronic_Only.ipynb`

The notebook is useful for manual or exploratory work. The production automation uses `scripts/scrape_senate_ptrs.py`.
