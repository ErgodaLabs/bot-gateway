# Quotes Scraper

An enterprise-grade Python web scraper for [quotes.toscrape.com](http://quotes.toscrape.com/).  
It crawls paginated quote pages, enforces `robots.txt` compliance, deduplicates results in memory, and exports data to SQLite, CSV, and/or JSON through a clean CLI.

Built as a portfolio project to demonstrate production-oriented scraping practices: OOP design, polite crawling, resilient HTTP handling, batch persistence, and an offline test suite.

---

## Key Features

- **OOP architecture** — `QuotesScraper` encapsulates session lifecycle, parsing, pagination, and export behind a context manager (`with QuotesScraper(...) as scraper`).
- **Strict `robots.txt` compliance** — Fetches and parses robots rules before any database work; disallowed crawls raise a custom `ScraperError` and abort cleanly.
- **Batch export pipeline** — Persist unique quotes to **SQLite**, **CSV**, **JSON**, or **all** in one run (`--export`).
- **In-memory deduplication** — Tracks seen quote texts with a `set` while preserving first-seen order in a `list` for stable exports.
- **Smart rate limiting** — Random delay between requests (`--rate-limit-min` / `--rate-limit-max`) to reduce server load and ban risk.
- **Resilient HTTP layer** — `urllib3.Retry` + `HTTPAdapter` retries on common failures and status codes `429`, `500`, `502`, `503`, `504`.
- **Two-tier error model**
  - **Hard failures** (`ScraperError`): robots disallowals / strict fetch errors → full abort.
  - **Soft failures**: page-fetch errors during pagination → loop stops gracefully; collected data can still be exported.
- **CLI-first configuration** — Paths, timeouts, pagination limits, export mode, and dry-run are all argparse-driven.
- **Progress & observability** — `tqdm` progress bar with logging-safe output; structured file + console logs.
- **Offline test suite** — `pytest` coverage for parsing, session lifecycle, robots guard, batch SQLite inserts, and in-memory dedup — no live network required.

---

## Tech Stack

| Layer | Tools |
|-------|--------|
| Language | Python 3.10+ |
| HTTP | `requests`, `urllib3.util.retry` |
| Parsing | `BeautifulSoup4` |
| Storage | `sqlite3` (stdlib), CSV, JSON |
| UX | `tqdm` |
| Testing | `pytest`, `unittest.mock` |

---

## Installation

```bash
git clone <your-repo-url>
cd quotes-scraper

python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

---

## Usage & CLI Options

### Common flags

| Flag | Description | Default |
|------|-------------|---------|
| `--base-url` | Target site base URL | `http://quotes.toscrape.com/` |
| `--db-path` | SQLite path; also used to derive `.csv` / `.json` paths | `quotes.db` |
| `--log-path` | Log file path | `scraper.log` |
| `--timeout` | HTTP timeout (seconds) | `15` |
| `--max-pages` | Cap pagination (`>= 1`); omit for unlimited | unlimited |
| `--rate-limit-min` | Minimum delay before each request (seconds) | `0.5` |
| `--rate-limit-max` | Maximum delay before each request (seconds) | `2.0` |
| `--export` | `sqlite` \| `csv` \| `json` \| `all` | `sqlite` |
| `--dry-run` | Crawl & parse only; skip DB/file writes | off |

Parent directories for DB/CSV/JSON/log paths are created automatically when needed.

### Examples

**Dry-run** (validate crawl/parse with no disk writes):

```bash
python scraper.py --dry-run --max-pages 2
```

**Export everything** (SQLite + CSV + JSON under `data/`):

```bash
python scraper.py --export all --db-path data/quotes.db --max-pages 5
```

**Polite CSV-only scrape** with tighter rate limits:

```bash
python scraper.py \
  --export csv \
  --db-path output/quotes.db \
  --rate-limit-min 1.0 \
  --rate-limit-max 2.5 \
  --timeout 20
```

> Note: CSV/JSON paths are derived from `--db-path`  
> (e.g. `data/quotes.db` → `data/quotes.csv` and `data/quotes.json`).

---

## Testing

The suite mocks all network calls and patches rate limiting so tests stay **offline and fast**.

```bash
cd quotes-scraper
pip install -r requirements.txt
pytest test_scraper.py -q
```

Covered areas include:

- Context-manager session init/close
- HTML parsing with malformed quote blocks
- In-memory deduplication before batch save
- SQLite `INSERT OR IGNORE` duplicate handling (`tmp_path` isolation)
- `robots.txt` disallow → `ScraperError`
- Dry-run skips persistence

---

## Project Structure

```text
quotes-scraper/
├── scraper.py          # Production scraper (CLI + pipeline)
├── test_scraper.py     # Offline pytest suite
├── requirements.txt    # Runtime + test dependencies
└── README.md           # Project documentation
```

---

## Design Notes

1. **Robots check runs first** in `run()`, before any database initialization.  
2. **SQLite is initialized only** when `--export` is `sqlite` or `all` and `--dry-run` is not set.  
3. **Pagination stop conditions** are explicit: no next link, `--max-pages` reached, or revisited URL (loop guard).  
4. **Batch inserts** use `executemany` + `INSERT OR IGNORE`; newly inserted rows are measured via `COUNT(*)` before/after.

---

## License

For portfolio / educational use. Respect target site terms and `robots.txt` when pointing `--base-url` at other hosts.
