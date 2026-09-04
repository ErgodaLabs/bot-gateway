"""
Offline pytest suite for QuotesScraper.

All network I/O is mocked. Rate limiting is patched so the suite stays fast.
"""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
import requests
from bs4 import BeautifulSoup

from scraper import USER_AGENT, Quote, QuotesScraper, ScraperError


HTML_FIXTURE = """
<html>
  <body>
    <div class="quote">
      <span class="text">“Only the valid quote should survive.”</span>
      <small class="author">Ada Lovelace</small>
    </div>
    <div class="quote">
      <small class="author">Missing Text Author</small>
    </div>
    <div class="quote">
      <span class="text">“Missing author should be skipped.”</span>
    </div>
  </body>
</html>
"""

ROBOTS_DISALLOW = "User-agent: *\nDisallow: /\n"
ROBOTS_ALLOW = "User-agent: *\nDisallow:\n"


@pytest.fixture
def html_soup() -> BeautifulSoup:
    """Return a BeautifulSoup document built from the static HTML fixture."""
    return BeautifulSoup(HTML_FIXTURE, "html.parser")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Return an isolated temporary SQLite database path."""
    return tmp_path / "quotes.db"


@pytest.fixture
def scraper(db_path: Path) -> Iterator[QuotesScraper]:
    """
    Yield a context-managed QuotesScraper with rate limiting disabled.

    The session is opened for the duration of the test and closed on teardown.
    """
    with patch.object(QuotesScraper, "_apply_rate_limit", return_value=None):
        with QuotesScraper(
            db_path=db_path,
            rate_limit_min=0.0,
            rate_limit_max=0.0,
            export_format="sqlite",
        ) as instance:
            yield instance


def test_context_manager_initializes_and_closes_session() -> None:
    """QuotesScraper must create a Session on enter and clear it on exit."""
    scraper = QuotesScraper(rate_limit_min=0.0, rate_limit_max=0.0)
    assert scraper.session is None

    with patch.object(requests.Session, "close", autospec=True) as mock_close:
        with scraper as active:
            assert active.session is not None
            assert isinstance(active.session, requests.Session)
            assert active.session.headers.get("User-Agent") == USER_AGENT
            session_ref = active.session

        mock_close.assert_called_once_with(session_ref)

    assert scraper.session is None


def test_parse_quotes_keeps_only_valid_entries(html_soup: BeautifulSoup) -> None:
    """parse_quotes should extract one valid Quote and ignore malformed blocks."""
    scraper = QuotesScraper(rate_limit_min=0.0, rate_limit_max=0.0)
    quotes = scraper.parse_quotes(html_soup)

    assert quotes == [
        Quote(text="“Only the valid quote should survive.”", author="Ada Lovelace")
    ]


def test_save_quotes_sqlite_insert_or_ignore_skips_duplicates(
    scraper: QuotesScraper,
) -> None:
    """
    Batch INSERT OR IGNORE must skip duplicate texts without raising.

    First batch inserts new rows; a second identical batch inserts zero rows.
    """
    scraper.init_database()
    quotes = [
        Quote(text="First unique quote", author="Author A"),
        Quote(text="Second unique quote", author="Author B"),
        Quote(text="First unique quote", author="Author A"),
    ]

    first_inserted = scraper.save_quotes_sqlite(quotes)
    second_inserted = scraper.save_quotes_sqlite(quotes)

    assert first_inserted == 2
    assert second_inserted == 0


def test_run_in_memory_dedup_passes_unique_quotes_to_save(
    scraper: QuotesScraper,
    html_soup: BeautifulSoup,
) -> None:
    """
    run() must deduplicate by text in memory before calling save_quotes_sqlite.

    Duplicate quote texts across pages should appear only once in the batch.
    """
    duplicate_soup = BeautifulSoup(
        """
        <div class="quote">
          <span class="text">“Only the valid quote should survive.”</span>
          <small class="author">Ada Lovelace</small>
        </div>
        <div class="quote">
          <span class="text">“A second unique quote.”</span>
          <small class="author">Grace Hopper</small>
        </div>
        """,
        "html.parser",
    )
    pages: list[tuple[str, BeautifulSoup]] = [
        ("http://example.test/page/1/", html_soup),
        ("http://example.test/page/2/", duplicate_soup),
    ]

    with (
        patch.object(scraper, "check_robots_txt", return_value=None),
        patch.object(scraper, "iter_pages", return_value=iter(pages)),
        patch.object(
            scraper,
            "save_quotes_sqlite",
            wraps=scraper.save_quotes_sqlite,
        ) as mock_save,
    ):
        scraper.run()

    mock_save.assert_called_once()
    saved_quotes: list[Quote] = mock_save.call_args.args[0]
    assert saved_quotes == [
        Quote(text="“Only the valid quote should survive.”", author="Ada Lovelace"),
        Quote(text="“A second unique quote.”", author="Grace Hopper"),
    ]


def test_check_robots_txt_raises_scraper_error_when_disallowed(
    scraper: QuotesScraper,
) -> None:
    """A disallow-all robots.txt must raise ScraperError."""
    with patch.object(
        scraper,
        "_fetch_raw_text",
        return_value=ROBOTS_DISALLOW,
    ):
        with pytest.raises(ScraperError, match="robots.txt disallows crawling"):
            scraper.check_robots_txt()


def test_check_robots_txt_allows_when_not_disallowed(
    scraper: QuotesScraper,
) -> None:
    """An allow-all robots.txt must not raise."""
    with patch.object(
        scraper,
        "_fetch_raw_text",
        return_value=ROBOTS_ALLOW,
    ):
        scraper.check_robots_txt()


def test_dry_run_skips_database_and_file_exports(
    db_path: Path,
    html_soup: BeautifulSoup,
) -> None:
    """Dry-run must crawl/parse without initializing DB or calling exporters."""
    pages: list[tuple[str, BeautifulSoup]] = [
        ("http://example.test/", html_soup),
    ]

    with patch.object(QuotesScraper, "_apply_rate_limit", return_value=None):
        with QuotesScraper(
            db_path=db_path,
            rate_limit_min=0.0,
            rate_limit_max=0.0,
            export_format="all",
            dry_run=True,
        ) as scraper:
            with (
                patch.object(scraper, "check_robots_txt", return_value=None),
                patch.object(scraper, "iter_pages", return_value=iter(pages)),
                patch.object(scraper, "init_database") as mock_init,
                patch.object(scraper, "save_quotes_sqlite") as mock_sqlite,
                patch.object(scraper, "export_csv") as mock_csv,
                patch.object(scraper, "export_json") as mock_json,
            ):
                scraper.run()

    mock_init.assert_not_called()
    mock_sqlite.assert_not_called()
    mock_csv.assert_not_called()
    mock_json.assert_not_called()
    assert not db_path.exists()
