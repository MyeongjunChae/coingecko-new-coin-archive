from datetime import datetime
from pathlib import Path

from coingecko_archive import (
    Coin,
    _browser_executable,
    age_within_days,
    deduplicate_coins,
    extract_labeled_link,
    is_complete,
    is_pdf_content,
    page_url,
    parse_listing_html,
    retry,
    _recent_sort_key,
    _save_preview,
    _save_full_page_screenshot,
    _capture_allowed,
    sanitize_filename,
)


def test_browser_resolution_selects_explicit_chrome_or_edge(tmp_path, monkeypatch):
    chrome = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome.parent.mkdir(parents=True)
    chrome.touch()
    edge = tmp_path / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.touch()
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    assert _browser_executable("chrome") == str(chrome)
    assert _browser_executable("edge") == str(edge)


def test_browser_resolution_fails_closed_when_selected_browser_missing(tmp_path, monkeypatch):
    for environment in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        monkeypatch.setenv(environment, str(tmp_path))

    assert _browser_executable("chrome") is None
    assert _browser_executable("edge") is None


def test_listing_explicit_portfolio_state_and_unknown_fail_closed():
    html = """
    <table><tbody>
      <tr><td><button aria-label="Remove from Portfolio"></button></td>
          <td><a href="/en/coins/active-coin">Active Coin</a></td></tr>
      <tr><td><button aria-label="Add to Portfolio"></button></td>
          <td><a href="/en/coins/inactive-coin">Inactive Coin</a></td></tr>
      <tr><td><button class="star-button"></button></td>
          <td><a href="/en/coins/unknown-coin">Unknown Coin</a></td></tr>
    </tbody></table>
    """
    coins = parse_listing_html(html, 1)
    assert [coin.portfolio for coin in coins] == ["active", "inactive", "unknown"]


def test_coingecko_star_icons_and_recent_age_are_parsed_from_rows():
    html = """
    <p><a href="/en/coins/not-a-table-row">Ignore summary link</a></p>
    <table><tbody>
      <tr><td><i class="far fa-star" data-action="click->coin-favorites#handleCoinFavourites"
          data-analytics-event="select_add_portfolio_coin_cta"></i></td>
          <td><a href="/en/coins/inactive">Inactive</a></td><td>about 4 hours</td></tr>
      <tr><td><i class="fas fa-star" data-action="click->coin-favorites#handleCoinFavourites"
          data-analytics-event="select_remove_portfolio_coin_cta"></i></td>
          <td><a href="/en/coins/active">Active</a></td><td>2 days</td></tr>
      <tr><td><i class="fa-star" data-action="click->coin-favorites#handleCoinFavourites"></i></td>
          <td><a href="/en/coins/unknown">Unknown</a></td><td>8 days</td></tr>
    </tbody></table>
    """
    coins = parse_listing_html(html, 1)
    assert [(coin.name, coin.portfolio, coin.added) for coin in coins] == [
        ("Inactive", "inactive", "about 4 hours"),
        ("Active", "active", "2 days"),
        ("Unknown", "unknown", "8 days"),
    ]
    assert [coin.name for coin in coins if age_within_days(coin.added, 7)] == [
        "Inactive",
        "Active",
    ]


def test_recent_age_filter_is_conservative_for_unknown_and_seven_day_boundary():
    assert age_within_days("6 days", 7)
    assert age_within_days("7 days", 7)
    assert not age_within_days("not available", 7)
    assert age_within_days("Oct 12, 2026", 7, now=datetime(2026, 10, 12, 12))


def test_recent_first_sort_uses_listing_age_not_page_order():
    newer = Coin("Recent", "https://example.com/recent", "unknown", 3, "1 hour")
    older = Coin("Older", "https://example.com/older", "unknown", 1, "6 days")
    assert sorted([older, newer], key=_recent_sort_key) == [newer, older]


def test_dry_run_preview_csv_is_utf8_and_contains_only_listing_metadata(tmp_path):
    path = tmp_path / "preview.csv"
    coins = [
        Coin("YAODIE 妖蝶", "https://www.coingecko.com/en/coins/yaodie", "unknown", 1, "4 hours")
    ]

    _save_preview(path, coins)

    content = path.read_text(encoding="utf-8-sig")
    assert "YAODIE 妖蝶" in content
    assert "unknown" in content
    assert "https://www.coingecko.com/en/coins/yaodie" in content
    assert "screenshot" not in content


def test_pagination_and_listing_deduplication():
    assert page_url(1).endswith("?page=1")
    assert page_url(10).endswith("?page=10")
    html = """
    <table><tr><button aria-pressed="true" aria-label="Portfolio star"></button>
    <a href="/en/coins/example">Example</a></tr>
    <tr><button aria-pressed="true" aria-label="Portfolio star"></button>
    <a href="https://www.coingecko.com/en/coins/example?ref=duplicate">Example</a></tr></table>
    """
    coins = parse_listing_html(html, 1)
    assert len(coins) == 1
    assert coins[0].url == "https://www.coingecko.com/en/coins/example"


def test_multiple_pages_keep_page_numbers_and_unique_coins():
    first = parse_listing_html(
        '<tr><button aria-pressed="true" aria-label="Portfolio star"></button>'
        '<a href="/en/coins/first">First</a></tr>',
        1,
    )
    second = parse_listing_html(
        '<tr><button aria-pressed="false" aria-label="Portfolio star"></button>'
        '<a href="/en/coins/second">Second</a></tr>',
        2,
    )
    coins = deduplicate_coins([*first, *second])
    assert [(coin.name, coin.page, coin.portfolio) for coin in coins] == [
        ("First", 1, "active"),
        ("Second", 2, "inactive"),
    ]


def test_conflicting_duplicate_star_states_become_unknown():
    rows = [
        Coin("Example", "https://www.coingecko.com/en/coins/example", "active", 1),
        Coin("Example", "https://www.coingecko.com/en/coins/example/", "inactive", 2),
    ]
    assert deduplicate_coins(rows)[0].portfolio == "unknown"


def test_labeled_links_reject_ambiguous_values():
    html = """
    <a href="https://one.example">Website</a>
    <a href="https://two.example" aria-label="Website"></a>
    """
    assert extract_labeled_link(html, {"Website"}, "https://www.coingecko.com/")[1] == "ambiguous"


def test_pdf_magic_bytes_and_filename_safety():
    assert is_pdf_content(b"%PDF-1.7\n")
    assert not is_pdf_content(b"<html>not a PDF</html>")
    assert sanitize_filename('CON') == "coin"
    assert sanitize_filename('A/B:C*?') == "A_B_C__"
    assert len(sanitize_filename("x" * 200)) == 100


def test_completion_and_bounded_retry():
    assert is_complete({"status": "completed"})
    assert not is_complete({"status": "needs_review"})
    attempts = 0

    def flaky():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary")
        return "ok"

    assert retry(flaky, attempts=2, delay=0) == "ok"
    assert attempts == 2


def test_capture_requires_active_star_unless_explicitly_overridden():
    active = Coin("Active", "https://example.com/active", "active", 1)
    inactive = Coin("Inactive", "https://example.com/inactive", "inactive", 1)
    unknown = Coin("Unknown", "https://example.com/unknown", "unknown", 1)

    assert _capture_allowed(active, include_unstarred=False)
    assert not _capture_allowed(inactive, include_unstarred=False)
    assert not _capture_allowed(unknown, include_unstarred=False)
    assert all(_capture_allowed(coin, include_unstarred=True) for coin in (active, inactive, unknown))


def test_full_page_screenshot_saves_atomically_and_cleans_temporary_file(tmp_path):
    class FakePage:
        def screenshot(self, *, path, full_page, timeout):
            assert full_page
            assert timeout == 90_000
            Path(path).write_bytes(b"png")

    target = tmp_path / "whitepapers" / "Example-whitepaper.png"
    _save_full_page_screenshot(FakePage(), target)

    assert target.read_bytes() == b"png"
    assert not target.with_name(target.stem + ".part" + target.suffix).exists()
