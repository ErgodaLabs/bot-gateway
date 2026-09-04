"""
Web scraper for quotes.toscrape.com.
Extracts quote text and author names and stores them in SQLite, CSV, and/or JSON.
"""

import argparse
import csv
import json
import logging
import random
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError, HTTPError, RequestException, Timeout
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm
from urllib3.util.retry import Retry

BASE_URL = "http://quotes.toscrape.com/"
DEFAULT_REQUEST_TIMEOUT = 15
USER_AGENT = "QuotesScraper/1.0 (portfolio project; educational use)"
RETRY_TOTAL = 3
RETRY_BACKOFF_FACTOR = 1
RETRY_STATUS_FORCELIST = (429, 500, 502, 503, 504)
RATE_LIMIT_MIN_SECONDS = 0.5
RATE_LIMIT_MAX_SECONDS = 2.0


class ScraperError(Exception):
    """Raised for hard scraper failures such as robots.txt disallowals or strict fetch errors."""


@dataclass(frozen=True)
class Quote:
    text: str
    author: str


class QuotesScraper:
    """Main scraper class handling HTTP requests, parsing, and batch exports."""

    def __init__(
        self,
        base_url: str = BASE_URL,
        db_path: Path | None = None,
        timeout: int = DEFAULT_REQUEST_TIMEOUT,
        rate_limit_min: float = RATE_LIMIT_MIN_SECONDS,
        rate_limit_max: float = RATE_LIMIT_MAX_SECONDS,
        max_pages: int | None = None,
        export_format: str = "sqlite",
        dry_run: bool = False,
    ) -> None:
        if rate_limit_min > rate_limit_max:
            raise ValueError(
                f"rate_limit_min ({rate_limit_min}) must be <= "
                f"rate_limit_max ({rate_limit_max})"
            )
        if max_pages is not None and max_pages <= 0:
            raise ValueError(f"max_pages must be >= 1 when set, got {max_pages}")

        self.base_url = base_url
        self.db_path = db_path or Path("quotes.db")
        self.timeout = timeout
        self.rate_limit_min = rate_limit_min
        self.rate_limit_max = rate_limit_max
        self.max_pages = max_pages
        self.export_format = export_format
        self.dry_run = dry_run
        self.session: requests.Session | None = None
        self.logger = logging.getLogger(self.__class__.__name__)

    def __enter__(self) -> "QuotesScraper":
        """Initialize the HTTP session with retry adapters."""
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        retry_strategy = Retry(
            total=RETRY_TOTAL,
            backoff_factor=RETRY_BACKOFF_FACTOR,
            status_forcelist=RETRY_STATUS_FORCELIST,
            allowed_methods=["HEAD", "GET", "OPTIONS"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        """Close the HTTP session regardless of success or failure."""
        if self.session is not None:
            self.session.close()
            self.session = None

    def _require_session(self) -> requests.Session:
        """Return the active session or raise if used outside a context manager."""
        if self.session is None:
            raise ScraperError(
                "QuotesScraper must be used as a context manager "
                "(with QuotesScraper(...) as scraper)."
            )
        return self.session

    def _apply_rate_limit(self) -> None:
        """Sleep for a random delay between configured rate-limit bounds."""
        delay = random.uniform(self.rate_limit_min, self.rate_limit_max)
        self.logger.debug(
            "Rate limiting: sleeping %.2fs before next request.",
            delay,
        )
        time.sleep(delay)

    def _fetch_raw_text(self, url: str) -> str:
        """
        Fetch a URL and return its response body as plain text.

        A 404 response yields an empty string. Other HTTP errors and network
        failures raise ScraperError.
        """
        session = self._require_session()
        try:
            response = session.get(url, timeout=self.timeout)
            response.raise_for_status()
            return response.text
        except HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return ""
            raise ScraperError(f"HTTP error while fetching {url}: {exc}") from exc
        except RequestException as exc:
            raise ScraperError(f"Request failed while fetching {url}: {exc}") from exc

    def check_robots_txt(self) -> None:
        """
        Fetch and parse robots.txt for the configured base URL.

        Raises ScraperError when crawling the base URL is disallowed for USER_AGENT.
        A missing robots.txt (HTTP 404) is treated as allow-all.
        """
        robots_url = urljoin(self.base_url, "/robots.txt")
        self.logger.info("Checking robots.txt at %s", robots_url)
        self._apply_rate_limit()
        text = self._fetch_raw_text(robots_url)

        rp = RobotFileParser()
        rp.parse(text.splitlines())

        if not rp.can_fetch(USER_AGENT, self.base_url):
            raise ScraperError(
                f"robots.txt disallows crawling {self.base_url} for user-agent "
                f"{USER_AGENT!r}"
            )
        self.logger.info("robots.txt allows crawling for %s", USER_AGENT)

    def fetch_page(self, url: str) -> BeautifulSoup:
        """Fetch an HTML page and return a BeautifulSoup document."""
        session = self._require_session()
        try:
            response = session.get(url, timeout=self.timeout)
            response.raise_for_status()
        except Timeout as exc:
            self.logger.error("Request timed out after %ss: %s", self.timeout, url)
            raise RequestException(f"Timeout fetching {url}") from exc
        except ConnectionError as exc:
            self.logger.error("Connection error while fetching %s: %s", url, exc)
            raise
        except HTTPError as exc:
            status = exc.response.status_code if exc.response else "?"
            self.logger.error("HTTP %s for %s", status, url)
            raise
        except RequestException as exc:
            self.logger.error("Request failed for %s: %s", url, exc)
            raise

        return BeautifulSoup(response.text, "html.parser")

    def parse_quotes(self, soup: BeautifulSoup) -> list[Quote]:
        """Extract quote text/author pairs from a parsed page."""
        quotes: list[Quote] = []

        for block in soup.select("div.quote"):
            text_el = block.select_one("span.text")
            author_el = block.select_one("small.author")

            if text_el is None or author_el is None:
                self.logger.warning("Skipping malformed quote block on page.")
                continue

            text = text_el.get_text(strip=True)
            author = author_el.get_text(strip=True)

            if not text or not author:
                self.logger.warning("Skipping quote with empty text or author.")
                continue

            quotes.append(Quote(text=text, author=author))

        return quotes

    def get_next_page_url(self, soup: BeautifulSoup, current_url: str) -> str | None:
        """Return the absolute URL of the next page, or None if pagination ends."""
        next_link = soup.select_one("li.next a")
        if next_link is None or not next_link.get("href"):
            return None
        return urljoin(current_url, next_link["href"])

    def iter_pages(self) -> Iterator[tuple[str, BeautifulSoup]]:
        """
        Yield (url, soup) for each scraped page.

        Stops with a distinct log message for: no next link, max-pages reached,
        or an infinite-loop (already-visited URL) detection. Page-fetch failures
        break the loop softly without raising.
        """
        url: str | None = self.base_url
        page_number = 1
        visited: set[str] = set()

        while url:
            if url in visited:
                self.logger.warning(
                    "Infinite loop detected: already visited %s. Stopping pagination.",
                    url,
                )
                break
            visited.add(url)

            self._apply_rate_limit()
            self.logger.info("Fetching page %d: %s", page_number, url)
            try:
                soup = self.fetch_page(url)
            except RequestException:
                self.logger.error(
                    "Stopping pagination due to fetch failure on page %d.",
                    page_number,
                )
                break

            yield url, soup

            if self.max_pages is not None and page_number >= self.max_pages:
                self.logger.info(
                    "Reached max-pages limit (%d). Stopping pagination.",
                    self.max_pages,
                )
                break

            next_url = self.get_next_page_url(soup, url)
            if next_url is None:
                self.logger.info("No next link found. Stopping pagination.")
                break

            url = next_url
            page_number += 1

    def init_database(self) -> None:
        """Create the quotes table and author index if they do not already exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS quotes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        text TEXT NOT NULL,
                        author TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE (text)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_quotes_author ON quotes (author)"
                )
                conn.commit()
            self.logger.info("Database initialized at %s", self.db_path.resolve())
        except sqlite3.Error as exc:
            self.logger.error("Failed to initialize database: %s", exc)
            raise

    def _export_paths(self) -> tuple[Path, Path]:
        """Derive CSV and JSON paths from the configured database path."""
        csv_path = self.db_path.with_suffix(".csv")
        json_path = self.db_path.with_suffix(".json")
        return csv_path, json_path

    def save_quotes_sqlite(self, quotes: list[Quote]) -> int:
        """
        Batch-insert quotes with INSERT OR IGNORE.

        Returns the number of newly inserted rows (COUNT after minus COUNT before).
        """
        if not quotes:
            return 0

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [(quote.text, quote.author) for quote in quotes]
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                before = cursor.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
                cursor.executemany(
                    "INSERT OR IGNORE INTO quotes (text, author) VALUES (?, ?)",
                    rows,
                )
                after = cursor.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
                conn.commit()
        except sqlite3.Error as exc:
            self.logger.error("Database error while saving quotes: %s", exc)
            raise

        return after - before

    def export_csv(self, quotes: list[Quote], path: Path) -> None:
        """Overwrite a UTF-8 CSV file with unique quotes (text, author)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["text", "author"])
            writer.writeheader()
            writer.writerows(asdict(quote) for quote in quotes)

    def export_json(self, quotes: list[Quote], path: Path) -> None:
        """Overwrite a UTF-8 JSON file with unique quotes as a list of objects."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(quote) for quote in quotes]
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    def _should_use_sqlite(self) -> bool:
        """Return True when SQLite export is requested and dry-run is inactive."""
        return self.export_format in ("sqlite", "all") and not self.dry_run

    def _should_use_csv(self) -> bool:
        """Return True when CSV export is requested and dry-run is inactive."""
        return self.export_format in ("csv", "all") and not self.dry_run

    def _should_use_json(self) -> bool:
        """Return True when JSON export is requested and dry-run is inactive."""
        return self.export_format in ("json", "all") and not self.dry_run

    def run(self) -> None:
        """
        Execute the full scrape pipeline: robots check, pagination, dedup, export.

        Hard failures (robots / strict fetch) raise ScraperError. Soft page-fetch
        failures stop pagination without aborting the process.
        """
        self.check_robots_txt()

        if self.dry_run:
            self.logger.warning(
                "Dry-run active: export flags are ignored; "
                "no database or file writes will be performed."
            )

        if self._should_use_sqlite():
            self.init_database()

        unique_quotes: list[Quote] = []
        seen_texts: set[str] = set()
        total_parsed_quotes = 0
        pages_scraped = 0

        with logging_redirect_tqdm():
            progress = tqdm(
                self.iter_pages(),
                total=self.max_pages,
                desc="Scraping pages",
                unit="page",
            )
            for _url, soup in progress:
                pages_scraped += 1
                page_quotes = self.parse_quotes(soup)
                total_parsed_quotes += len(page_quotes)

                for quote in page_quotes:
                    if quote.text in seen_texts:
                        continue
                    seen_texts.add(quote.text)
                    unique_quotes.append(quote)

                self.logger.info(
                    "Page %d: parsed %d quote(s).",
                    pages_scraped,
                    len(page_quotes),
                )

        total_unique_quotes = len(unique_quotes)
        self.logger.info(
            "Scrape metrics — pages: %d, total_parsed_quotes: %d, "
            "total_unique_quotes: %d.",
            pages_scraped,
            total_parsed_quotes,
            total_unique_quotes,
        )

        if self.dry_run:
            self.logger.warning(
                "Dry-run complete: disk operations skipped "
                "(%d unique quote(s) collected in memory).",
                total_unique_quotes,
            )
            return

        csv_path, json_path = self._export_paths()
        exported_to_files = False

        if self._should_use_csv():
            self.export_csv(unique_quotes, csv_path)
            exported_to_files = True

        if self._should_use_json():
            self.export_json(unique_quotes, json_path)
            exported_to_files = True

        if exported_to_files:
            self.logger.info(
                "Exported %d unique quotes to file(s).",
                total_unique_quotes,
            )

        if self._should_use_sqlite():
            inserted = self.save_quotes_sqlite(unique_quotes)
            self.logger.info("Inserted %d new rows to DB.", inserted)


def setup_logging(log_path: Path) -> None:
    """Configure file and console logging with the project format."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _positive_int(value: str) -> int:
    """argparse type: require an integer >= 1."""
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(
            f"must be an integer >= 1, got {parsed}"
        )
    return parsed


def _non_negative_float(value: str) -> float:
    """argparse type: require a float >= 0."""
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError(
            f"must be a float >= 0, got {parsed}"
        )
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the scraper pipeline."""
    parser = argparse.ArgumentParser(
        description="Scrape quotes from quotes.toscrape.com into SQLite/CSV/JSON.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path("quotes.db"),
        help="Path to the SQLite database file (also used to derive CSV/JSON paths).",
    )
    parser.add_argument(
        "--log-path",
        type=Path,
        default=Path("scraper.log"),
        help="Path to the log file (default: scraper.log).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_REQUEST_TIMEOUT,
        help=f"HTTP request timeout in seconds (default: {DEFAULT_REQUEST_TIMEOUT}).",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=BASE_URL,
        help=f"Target site base URL (default: {BASE_URL}).",
    )
    parser.add_argument(
        "--max-pages",
        type=_positive_int,
        default=None,
        help="Maximum number of pages to scrape (default: unlimited).",
    )
    parser.add_argument(
        "--rate-limit-min",
        type=_non_negative_float,
        default=RATE_LIMIT_MIN_SECONDS,
        help=f"Minimum delay in seconds before each request (default: {RATE_LIMIT_MIN_SECONDS}).",
    )
    parser.add_argument(
        "--rate-limit-max",
        type=_non_negative_float,
        default=RATE_LIMIT_MAX_SECONDS,
        help=f"Maximum delay in seconds before each request (default: {RATE_LIMIT_MAX_SECONDS}).",
    )
    parser.add_argument(
        "--export",
        choices=["sqlite", "csv", "json", "all"],
        default="sqlite",
        help="Export destination(s) (default: sqlite).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Crawl and parse without writing to the database or export files.",
    )
    args = parser.parse_args(argv)

    if args.rate_limit_min > args.rate_limit_max:
        parser.error(
            f"--rate-limit-min ({args.rate_limit_min}) must be <= "
            f"--rate-limit-max ({args.rate_limit_max})"
        )

    return args


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    args = parse_args(argv)
    setup_logging(args.log_path)
    logger = logging.getLogger(__name__)
    logger.info("Starting quotes scraper.")

    try:
        with QuotesScraper(
            base_url=args.base_url,
            db_path=args.db_path,
            timeout=args.timeout,
            rate_limit_min=args.rate_limit_min,
            rate_limit_max=args.rate_limit_max,
            max_pages=args.max_pages,
            export_format=args.export,
            dry_run=args.dry_run,
        ) as scraper:
            scraper.run()
    except ScraperError as exc:
        logger.error("Scraper aborted: %s", exc)
        return 1
    except sqlite3.Error:
        logger.error("Scraper aborted due to a database error.")
        return 1
    except RequestException:
        logger.error("Scraper aborted due to a network error.")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
