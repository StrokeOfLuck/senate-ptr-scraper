# Senate PTR Scraper

Scrapes official electronic Senate Periodic Transaction Reports (PTRs) from
the Senate's Electronic Financial Disclosure (eFD) system at
[efdsearch.senate.gov](https://efdsearch.senate.gov).

## What it does

- Searches the official Senate eFD PTR index for a configurable date range.
- Identifies both electronic (`/ptr/`) and paper (`/paper/`) filings.
- **Scrapes only electronic PTRs** -- parses the official HTML transaction
  table for each filing.
- Saves the official HTML, a filing index, a transaction CSV, and a scrape
  status CSV.
- Resumes from an existing transaction CSV on subsequent runs instead of
  re-scraping everything.

Paper filings are intentionally **not** downloaded or parsed here. They're
kept in the filing index and marked `paper_deferred` in the status file, to
be handled by a separate project later.

## Automated scraping (GitHub Actions)

`scripts/scrape_senate_ptrs.py` is the automation-friendly version of the
pipeline, run daily by `.github/workflows/scrape.yml` (~9:30am US Eastern).
It writes data into `data/` in this repo:

```
data/
|-- 01_html/                 raw HTML per electronic filing
|-- 02_filing_index/         senate_ptr_filing_index.csv
|-- 03_transactions/         senate_ptr_transactions_electronic.csv
`-- 04_status/               senate_ptr_scrape_status.csv
```

The workflow commits any data changes back to this repo, then (if the
`SITE_DISPATCH_TOKEN` secret is configured) notifies the `sean-data-portfolio`
site repo to rebuild and redeploy with the fresh data.

Config is overridable via environment variables (see the top of
`scripts/scrape_senate_ptrs.py`): `SENATE_PTR_START_DATE`,
`SENATE_PTR_END_DATE`, `SENATE_PTR_BATCH_SIZE`, `SENATE_PTR_REQUEST_DELAY`,
`SENATE_PTR_MAX_SEARCH_PAGES`, `SENATE_PTR_SAVE_HTML`,
`SENATE_PTR_RESUME_EXISTING`.

Setup steps that must be done manually in the GitHub UI (repo permissions,
the dispatch token, GitHub Pages) are documented in
`sean-data-portfolio/AUTOMATION_SETUP.md`.

## Notebook (manual/exploratory use)

[`notebooks/Scrape_Official_Senate_PTRs_Electronic_Only.ipynb`](notebooks/Scrape_Official_Senate_PTRs_Electronic_Only.ipynb)

The original Colab notebook this script was ported from. Still useful for
manual/exploratory runs (e.g. archiving a personal copy of the data to
Google Drive) -- mounts Google Drive for storage and installs its own
dependencies in Cell 1. Not used by the automated pipeline.

## Requirements

See `requirements.txt` (`beautifulsoup4`, `lxml`, `openpyxl`, `pandas`,
`requests`).

## Notes

- Runs on standard GitHub Actions runners -- no GPU/accelerator needed.
- Respects a configurable `SENATE_PTR_REQUEST_DELAY` between requests to
  the Senate site; increase this if you hit HTTP 403 responses (IP-level
  blocking).
- The search pagination loop has a hard `SENATE_PTR_MAX_SEARCH_PAGES`
  safety cap to prevent runaway loops if the Senate API response shape
  ever changes.
- Raw per-filing HTML is committed to git by design (kept for archival
  purposes); this is expected to stay well within GitHub's soft repo-size
  guidance given Senate's filing volume, but worth monitoring over time.
