"""
Web scraper for quotes.toscrape.com.
Extracts quote text and author names and stores them in a local SQLite database.
"""

import argparse
import logging
import random
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError, HTTPError, RequestException, Timeout
from urllib3.util.retry import Retry

BASE_URL = "http://quotes.toscrape.com/"
DEFAULT_REQUEST_TIMEOUT = 15
USER_AGENT = "QuotesScraper/1.0 (portfolio project; educational use)"
RETRY_TOTAL = 3
RETRY_BACKOFF_FACTOR = 1
RETRY_STATUS_FORCELIST = (429, 500, 502, 503, 504)
RATE_LIMIT_MIN_SECONDS = 0.5
RATE_LIMIT_MAX_SECONDS = 2.0


@dataclass(frozen=True)
class Quote:
    text: str
    author: str


class QuotesScraper:
    """Main scraper class handling HTTP requests, parsing, and database operations."""

    def __init__(
        self,
        base_url: str = BASE_URL,
        db_path: Path | None = None,
        timeout: int = DEFAULT_REQUEST_TIMEOUT,
        rate_limit_min: float = RATE_LIMIT_MIN_SECONDS,
        rate_limit_max: float = RATE_LIMIT_MAX_SECONDS,
    ) -> None:
        self.base_url = base_url
        self.db_path = db_path or Path("quotes.db")
        self.timeout = timeout
        self.rate_limit_min = rate_limit_min
        self.rate_limit_max = rate_limit_max

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
        self.logger = logging.getLogger(self.__class__.__name__)

    def _apply_rate_limit(self) -> None:
        delay = random.uniform(self.rate_limit_min, self.rate_limit_max)
        self.logger.debug(
            "Rate limiting: sleeping %.2fs before next request.",
            delay,
        )
        time.sleep(delay)

    def fetch_page(self, url: str) -> BeautifulSoup:
        try:
            response = self.session.get(url, timeout=self.timeout)
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
        next_link = soup.select_one("li.next a")
        if next_link is None or not next_link.get("href"):
            return None
        return urljoin(current_url, next_link["href"])

    def iter_pages(self) -> Iterator[tuple[str, BeautifulSoup]]:
        url: str | None = self.base_url
        page_number = 1

        while url:
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

            url = self.get_next_page_url(soup, url)
            page_number += 1

    def init_database(self) -> None:
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

    def save_quotes(self, quotes: list[Quote]) -> int:
        if not quotes:
            return 0

        inserted = 0
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                for quote in quotes:
                    try:
                        cursor.execute(
                            "INSERT INTO quotes (text, author) VALUES (?, ?)",
                            (quote.text, quote.author),
                        )
                        inserted += 1
                    except sqlite3.IntegrityError:
                        self.logger.debug("Duplicate skipped: %.60s...", quote.text)
                conn.commit()
        except sqlite3.Error as exc:
            self.logger.error("Database error while saving quotes: %s", exc)
            raise

        return inserted

    def run(self) -> dict[str, int]:
        self.init_database()

        pages_scraped = 0
        quotes_found = 0
        quotes_saved = 0

        for _url, soup in self.iter_pages():
            pages_scraped += 1
            page_quotes = self.parse_quotes(soup)
            quotes_found += len(page_quotes)

            try:
                saved_on_page = self.save_quotes(page_quotes)
            except sqlite3.Error:
                self.logger.error("Failed to save quotes from page %d.", pages_scraped)
                continue

            quotes_saved += saved_on_page
            self.logger.info(
                "Page %d: parsed %d quote(s), saved %d new quote(s).",
                pages_scraped,
                len(page_quotes),
                saved_on_page,
            )

        self.logger.info(
            "Scrape complete — pages: %d, found: %d, newly saved: %d.",
            pages_scraped,
            quotes_found,
            quotes_saved,
        )

        return {
            "pages_scraped": pages_scraped,
            "quotes_found": quotes_found,
            "quotes_saved": quotes_saved,
        }


def setup_logging(log_path: Path) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape quotes from quotes.toscrape.com into a SQLite database.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path("quotes.db"),
        help="Path to the SQLite database file (default: quotes.db).",
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_path)
    logger = logging.getLogger(__name__)
    logger.info("Starting quotes scraper.")

    scraper = QuotesScraper(
        db_path=args.db_path,
        timeout=args.timeout,
    )

    try:
        summary = scraper.run()
    except sqlite3.Error:
        logger.error("Scraper aborted due to a database error.")
        return 1
    except RequestException:
        logger.error("Scraper aborted due to a network error.")
        return 1
    finally:
        scraper.session.close()

    logger.info(
        "Finished — %d page(s), %d quote(s) found, %d new row(s) inserted.",
        summary["pages_scraped"],
        summary["quotes_found"],
        summary["quotes_saved"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
