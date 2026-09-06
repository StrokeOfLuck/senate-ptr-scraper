# Senate PTR Scraper

Scrapes official electronic Senate Periodic Transaction Reports (PTRs) from
the Senate's Electronic Financial Disclosure (eFD) system at
[efdsearch.senate.gov](https://efdsearch.senate.gov).

## What it does

- Searches the official Senate eFD PTR index for a configurable date range.
- Identifies both electronic (`/ptr/`) and paper (`/paper/`) filings.
- **Scrapes only electronic PTRs** — parses the official HTML transaction
  table for each filing.
- Saves the official HTML, a filing index, a transaction CSV, a scrape
  status CSV, and a combined Excel workbook.
- Resumes from an existing transaction CSV on subsequent runs instead of
  re-scraping everything.

Paper filings are intentionally **not** downloaded or parsed here. They're
kept in the filing index and marked `paper_deferred` in the status file, to
be handled by a separate project later.

## Notebook

[`notebooks/Scrape_Official_Senate_PTRs_Electronic_Only.ipynb`](notebooks/Scrape_Official_Senate_PTRs_Electronic_Only.ipynb)

Designed to run in Google Colab, mounting Google Drive for storage. Expects
(and creates, if missing) the following folder structure in Drive:

```
Congressional Trading Data/Senate_PTRs/
├── 01 Official Senate PTR HTML/
├── 02 Senate PTR Filing Indexes/
├── 03 Parsed Senate PTR Transaction Data/
├── 04 Scrape Checkpoints and Status/
└── 05 Workflow Notebooks/          <- notebook lives here in Drive
```

Scraped data (raw HTML, CSVs, Excel workbook) is treated as **output**, not
source-controlled — see `.gitignore`. Only the notebook itself is tracked
here.

## Requirements

Installed inside the notebook itself (Cell 1):

- `beautifulsoup4`
- `lxml`
- `openpyxl`
- `pandas`, `requests` (available by default in Colab)

## Notes

- Runs on a CPU Colab runtime — no GPU/accelerator needed.
- Respects a configurable `REQUEST_DELAY` between requests to the Senate
  site; increase this if you hit HTTP 403 responses (IP-level blocking).
- The search pagination loop has a hard `MAX_SEARCH_PAGES` safety cap to
  prevent runaway loops if the Senate API response shape ever changes.
