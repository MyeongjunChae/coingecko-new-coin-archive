import base64
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import coingecko_archive as archive
import web_app as web
from coingecko_archive import (
    Coin,
    _browser_executable,
    _load_selection,
    age_within_days,
    assign_duplicate_indices,
    deduplicate_coins,
    extract_coin_website,
    extract_explorer_links,
    extract_labeled_link,
    extract_twitter_link,
    is_complete,
    is_pdf_content,
    output_path,
    page_url,
    parse_listing_html,
    read_devtools_active_port,
    retry,
    _recent_sort_key,
    _save_preview,
    _save_full_page_screenshot,
    _save_page_as_pdf,
    _capture_allowed,
    _is_authenticated,
    _twitter_account_unavailable,
    _capture_coin,
    _auto_add_inactive_coins,
    is_tokenized_stock_name,
    sanitize_filename,
)


def test_web_app_defaults_to_all_rows_from_selected_pages():
    pages, since_days, *_ = web._validate({})

    assert pages == 10
    assert since_days == 0


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


def test_open_whitepaper_sites_uses_browser_context_tabs_when_available(tmp_path):
    opened = []

    class FakePage:
        def goto(self, url, **kwargs):
            opened.append(url)

        def wait_for_timeout(self, *_args, **_kwargs):
            return None

    class FakeContext:
        def new_page(self):
            return FakePage()

    archive._open_whitepaper_sites(
        "chrome",
        tmp_path,
        ["https://example.com/doc-a", "https://example.com/doc-b"],
        context=FakeContext(),
    )

    assert opened == ["https://example.com/doc-a", "https://example.com/doc-b"]


def test_large_pdf_is_optimized_with_best_effort_external_tool(monkeypatch, tmp_path):
    target = tmp_path / "doc.pdf"
    target.write_bytes(b"%PDF-1.4\n" + b"X" * 4096)
    compressed = tmp_path / "doc.compressed.pdf"
    compressed.write_bytes(b"%PDF-1.4\n" + b"Y" * 512)

    monkeypatch.setattr(archive.shutil, "which", lambda name: "convert" if name in {"magick", "convert"} else None)

    def fake_run(command, **kwargs):
        assert command[0] == "convert"
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(archive.subprocess, "run", fake_run)

    archive._compress_pdf_if_needed(target, threshold_bytes=1)
    assert target.exists()
    assert target.read_bytes().startswith(b"%PDF-1.4")


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


def test_capture_allowed_requires_successful_addition_by_default():
    inactive = Coin("Inactive", "https://example.com/inactive", "inactive", 1)
    unknown = Coin("Unknown", "https://example.com/unknown", "unknown", 1)
    assert not _capture_allowed(inactive, False)
    assert not _capture_allowed(unknown, False)
    assert _capture_allowed(inactive, False, auto_added=True)
    assert not _capture_allowed(Coin("Already Active", "https://example.com/active", "active", 1), False)
    assert all(_capture_allowed(coin, True) for coin in (inactive, unknown))


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


def test_coin_link_rows_select_website_community_twitter_and_explorers():
    html = """
    <div class="row">
      <div>Website</div>
      <div><a href="https://example.org">example.org</a>
           <a href="https://example.org/docs">Whitepaper</a></div>
    </div>
    <div class="row">
      <div>Community</div>
      <div><a href="https://twitter.com/project">Twitter</a></div>
    </div>
    <div class="row">
      <div>Search on Twitter</div>
      <div><a href="https://twitter.com/search?q=coin">Twitter</a></div>
    </div>
    <div class="row">
      <div>Explorers</div>
      <div><a href="https://etherscan.io/token/1">Etherscan</a>
           <a href="https://etherscan.io/token/1#code">Etherscan source</a>
           <a href="https://blockscout.com/token/1">Blockscout</a></div>
    </div>
    """

    assert extract_coin_website(html, "https://www.coingecko.com/en/coins/example") == (
        "https://example.org",
        "ok",
    )
    assert extract_twitter_link(html, "https://www.coingecko.com/en/coins/example") == (
        "https://twitter.com/project",
        "ok",
    )
    assert extract_explorer_links(html, "https://www.coingecko.com/en/coins/example") == [
        "https://blockscout.com/token/1",
        "https://etherscan.io/token/1#code",
    ]


def test_explorer_fallback_scans_known_hosts_without_labeled_row():
    html = """
    <a href="https://etherscan.io/token/1">Token explorer</a>
    <a href="https://example.org">Project website</a>
    """

    assert extract_explorer_links(html, "https://www.coingecko.com/en/coins/example") == [
        "https://etherscan.io/token/1"
    ]


def test_deleted_twitter_page_is_detected_but_login_wall_is_not_misclassified():
    class FakePage:
        def __init__(self, text):
            self.text = text

        class Locator:
            def __init__(self, text):
                self.text = text

            def inner_text(self, timeout):
                return self.text

        def locator(self, selector):
            assert selector == "body"
            return self.Locator(self.text)

    assert _twitter_account_unavailable(FakePage("This account doesn't exist"))
    assert _twitter_account_unavailable(FakePage("This account has been suspended"))
    assert not _twitter_account_unavailable(FakePage("Log in to X to see this profile"))


def test_capture_uses_community_twitter_when_website_is_missing(tmp_path, monkeypatch):
    class FakePage:
        def screenshot(self, *, path, full_page, timeout):
            Path(path).write_bytes(b"png")

        class Body:
            def inner_text(self, timeout):
                return "Public profile"

        def locator(self, selector):
            assert selector == "body"
            return self.Body()

    monkeypatch.setattr(
        archive,
        "_coin_details",
        lambda page, coin: (
            coin.name, None, "missing", None, "missing",
            "https://twitter.com/example", "ok", ["https://etherscan.io/token/1"],
        ),
    )
    monkeypatch.setattr(archive, "_navigate", lambda page, url: SimpleNamespace(status=200))

    details, pending = _capture_coin(
        FakePage(), Coin("Example", "https://coingecko.com/en/coins/example", "unknown", 1),
        tmp_path / "screenshots", tmp_path / "whitepapers", tmp_path,
    )

    assert details["capture_source"] == "twitter"
    assert details["twitter_screenshot"].endswith("-twitter.png")
    assert "screenshot" not in details
    assert pending is None


def test_capture_skips_deleted_twitter_and_uses_explorer(tmp_path, monkeypatch):
    class FakePage:
        def screenshot(self, *, path, full_page, timeout):
            Path(path).write_bytes(b"png")

        class Body:
            def inner_text(self, timeout):
                return "This account doesn't exist"

        def locator(self, selector):
            assert selector == "body"
            return self.Body()

    visited = []
    monkeypatch.setattr(
        archive,
        "_coin_details",
        lambda page, coin: (
            coin.name, None, "missing", None, "missing",
            "https://twitter.com/deleted", "ok", ["https://etherscan.io/token/1"],
        ),
    )

    def navigate(page, url):
        visited.append(url)
        return SimpleNamespace(status=404 if "twitter.com" in url else 200)

    monkeypatch.setattr(archive, "_navigate", navigate)
    details, _ = _capture_coin(
        FakePage(), Coin("Example", "https://coingecko.com/en/coins/example", "unknown", 1),
        tmp_path / "screenshots", tmp_path / "whitepapers", tmp_path,
    )

    assert details["twitter_status"] == "account_unavailable"
    assert "twitter_screenshot" not in details
    assert details["capture_source"] == "explorer"
    assert details["explorer_screenshots"][0].endswith("-explorer.png")
    assert visited == ["https://twitter.com/deleted", "https://etherscan.io/token/1"]


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


def test_capture_targets_unstarred_coins_by_default_and_all_when_explicitly_requested():
    active = Coin("Active", "https://example.com/active", "active", 1)
    inactive = Coin("Inactive", "https://example.com/inactive", "inactive", 1)
    unknown = Coin("Unknown", "https://example.com/unknown", "unknown", 1)

    assert not _capture_allowed(active, include_unstarred=False)
    assert not _capture_allowed(inactive, include_unstarred=False)
    assert not _capture_allowed(unknown, include_unstarred=False)
    assert _capture_allowed(inactive, include_unstarred=False, auto_added=True)
    assert _capture_allowed(active, include_unstarred=False, auto_added=True)
    assert all(_capture_allowed(coin, include_unstarred=True) for coin in (active, inactive, unknown))
    tokenized = Coin(
        "Example Tokenized Stock",
        "https://example.com/tokenized",
        "inactive",
        1,
        symbol="ETS",
    )
    assert not _capture_allowed(tokenized, include_unstarred=True, auto_added=True)


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


def test_rendered_whitepaper_pdf_is_validated_and_saved_atomically(tmp_path):
    class FakeSession:
        def send(self, method, options):
            assert method == "Page.printToPDF"
            assert options["printBackground"]
            content = b"%PDF-1.7\n1 0 obj << /Type /Page >> endobj\n" + b"x" * 1100 + b"\n%%EOF"
            return {"data": base64.b64encode(content).decode("ascii")}

        def detach(self):
            pass

    class FakeContext:
        def new_cdp_session(self, page):
            return FakeSession()

    class FakePage:
        context = FakeContext()

    target = tmp_path / "whitepapers" / "GitBook-whitepaper.pdf"
    _save_page_as_pdf(FakePage(), target)

    assert target.read_bytes().startswith(b"%PDF-1.7\n")
    assert b"/Type /Page" in target.read_bytes()
    assert not target.with_name(target.stem + ".part" + target.suffix).exists()


def test_listing_extracts_symbol_and_cleans_duplicated_visible_name():
    html = """
    <table><tbody>
      <tr><td><i class="far fa-star" data-action="click->coin-favorites#handleCoinFavourites"
          data-analytics-event="select_add_portfolio_coin_cta"></i></td>
          <td><a href="/en/coins/whale-4"><img alt="WHALE">
            <div><div>WHALE<div>WHALE</div></div></div></a></td>
          <td>about 5 hours</td></tr>
      <tr><td><i class="far fa-star" data-action="click->coin-favorites#handleCoinFavourites"
          data-analytics-event="select_add_portfolio_coin_cta"></i></td>
          <td><a href="/en/coins/auren-protocol"><img alt="AU">
            <div><div>Auren Protocol<div>AU</div></div></div></a></td>
          <td>about 7 hours</td></tr>
    </tbody></table>
    """
    coins = parse_listing_html(html, 1)
    assert [(coin.name, coin.symbol, coin.label()) for coin in coins] == [
        ("WHALE", "WHALE", "WHALE [whale-4]"),
        ("Auren Protocol", "AU", "Auren Protocol (AU) [auren-protocol]"),
    ]


class _FakeVisibleLocator:
    def __init__(self, visible):
        self.visible = visible

    @property
    def first(self):
        return self

    def is_visible(self, timeout=None):
        return self.visible


class _FakeBodyLocator:
    def __init__(self, text):
        self.text = text

    def inner_text(self, timeout=None):
        return self.text


class _FakeAuthPage:
    def __init__(self, sign_in_visible, body_text):
        self.sign_in_visible = sign_in_visible
        self.body_text = body_text

    def locator(self, selector):
        if selector.startswith("button["):
            return _FakeVisibleLocator(self.sign_in_visible)
        return _FakeBodyLocator(self.body_text)


def test_authentication_detection_uses_sign_in_modal_and_labels():
    assert _is_authenticated(_FakeAuthPage(True, "Portfolio")) is False
    assert _is_authenticated(_FakeAuthPage(False, "Home\nLog in")) is False
    assert _is_authenticated(_FakeAuthPage(False, "Home\nCreate an account")) is False
    assert _is_authenticated(_FakeAuthPage(False, "Home\nPortfolio\nCoins")) is True


def test_listing_fallback_keeps_symbol_when_signed_out(monkeypatch):
    coin = Coin(
        "Auren Protocol",
        "https://www.coingecko.com/en/coins/auren-protocol",
        "active",
        1,
        "about 1 hour",
        "AU",
    )
    monkeypatch.setattr(archive, "_navigate", lambda page, url: None)
    monkeypatch.setattr(archive, "parse_listing_html", lambda html, number, url: [coin])
    monkeypatch.setattr(archive, "_is_authenticated", lambda page: False)
    page = SimpleNamespace(
        wait_for_selector=lambda *args, **kwargs: None,
        content=lambda: "<html></html>",
    )
    result = archive._listing_page(page, 1)
    assert result[0].symbol == "AU"
    assert result[0].portfolio == "unknown"
    assert result[0].label() == "Auren Protocol (AU) [auren-protocol]"


def test_duplicate_name_and_symbol_get_visible_numbered_suffix():
    coins = [
        Coin("Example", "https://www.coingecko.com/en/coins/example", "unknown", 1, "1 hour", "EX"),
        Coin("Example", "https://www.coingecko.com/en/coins/example-2", "unknown", 1, "2 hours", "EX"),
    ]
    labeled = assign_duplicate_indices(coins)
    assert [coin.label() for coin in labeled] == [
        "Example (EX) [example]",
        "Example (EX) #2 [example-2]",
    ]
    assert labeled[0].as_dict()["duplicate_index"] == 1
    assert labeled[1].as_dict()["duplicate_index"] == 2


def test_output_path_contains_symbol_and_slug_separator(tmp_path):
    target = output_path(
        tmp_path,
        "Whale Coin",
        "https://www.coingecko.com/en/coins/whale-4|https://example.org",
        "-website.png",
        "WHALE",
    )
    assert "Whale Coin (WHALE)" in target.name
    assert "whale-4" in target.name
    assert target.name.endswith("-website.png")


def test_output_path_includes_duplicate_suffix_for_same_name_entries(tmp_path):
    target = output_path(
        tmp_path,
        "Example",
        "https://www.coingecko.com/en/coins/example-2",
        "-website.png",
        "EX",
        duplicate_index=2,
    )
    assert "Example (EX) #2" in target.name
    assert "example-2" in target.name


def test_whitepaper_fallback_keeps_only_pdf_artifact(tmp_path, monkeypatch):
    class FakeSession:
        def send(self, method, options):
            content = b"%PDF-1.7\n1 0 obj << /Type /Page >> endobj\n%%EOF"
            return {"data": base64.b64encode(content).decode("ascii")}

        def detach(self):
            pass

    class FakeContext:
        def new_cdp_session(self, page):
            return FakeSession()

    class FakePage:
        context = FakeContext()

    monkeypatch.setattr(
        archive,
        "_coin_details",
        lambda page, coin: (
            coin.name,
            "https://example.com",
            "ok",
            "https://example.com/doc",
            "ok",
            None,
            "missing",
            [],
        ),
    )
    monkeypatch.setattr(archive, "_navigate", lambda page, url: SimpleNamespace(status=200))
    monkeypatch.setattr(archive, "_load_lazy_content", lambda page: None)
    monkeypatch.setattr(archive, "_download_pdf", lambda url, target: (False, "not_pdf:text/html"))

    def fake_render(page, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n%%EOF")

    monkeypatch.setattr(archive, "_save_page_as_pdf", fake_render)

    details, pending = archive._capture_coin(
        FakePage(),
        Coin("Example", "https://coingecko.com/en/coins/example", "unknown", 1),
        tmp_path / "screenshots",
        tmp_path / "whitepapers",
        tmp_path,
    )

    assert pending is None
    assert "whitepaper_screenshot" not in details
    assert details["pdf"].endswith("-whitepaper.pdf")
    assert not (tmp_path / "whitepapers" / "Example-whitepaper.png").exists()


def test_tokenized_stock_names_are_recognized_for_favorites_only_handling():
    assert is_tokenized_stock_name("Tokenized Stock", "TST")
    assert is_tokenized_stock_name("bStocks Tokenized Stock", "BST")
    assert not is_tokenized_stock_name("Example Coin", "EX")


def test_add_coins_to_favorites_uses_latest_portfolio_and_skips_duplicate_inputs(monkeypatch):
    coins = [
        Coin("Example", "https://www.coingecko.com/en/coins/example", "inactive", 1, "1 hour", "EX"),
        Coin(
            "Example Tokenized Stock",
            "https://www.coingecko.com/en/coins/example-tokenized-stock",
            "inactive",
            1,
            "1 hour",
            "ETS",
        ),
        Coin("Already Saved", "https://www.coingecko.com/en/coins/already-saved", "active", 1),
        Coin("Example", "https://www.coingecko.com/en/coins/example", "inactive", 1, "1 hour", "EX"),
    ]
    opened = []
    selected_folders = []
    monkeypatch.setattr(archive, "_navigate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(archive, "_is_authenticated", lambda _page: True)
    monkeypatch.setattr(
        archive,
        "_open_coin_portfolio_picker",
        lambda _page, coin: opened.append(coin.url) or "opened",
    )
    monkeypatch.setattr(
        archive,
        "_portfolio_options",
        lambda _page: [{"number": 16, "href": "/en/portfolios/124912956", "label": "⭐ 16"}],
    )
    monkeypatch.setattr(archive, "_portfolio_asset_urls", lambda *_args: set())
    monkeypatch.setattr(
        archive,
        "_click_portfolio_row_plus",
        lambda _page, number: selected_folders.append(number)
        or {"found": True, "clicked": True, "verified": True},
    )
    monkeypatch.setattr(archive, "_coin_detail_portfolio_state", lambda _page: "active")

    page = SimpleNamespace()
    results = archive.add_coins_to_favorites(page, coins)

    assert [result["status"] for result in results] == [
        "added",
        "added",
        "already_active",
        "duplicate_input",
    ]
    assert [result.get("portfolio_folder") for result in results] == [16, 16, None, None]
    assert opened == [coins[0].url, coins[0].url, coins[1].url]
    assert selected_folders == [16, 16]


def test_add_coins_to_favorites_creates_next_portfolio_when_latest_is_full(monkeypatch):
    coin = Coin("Example", "https://www.coingecko.com/en/coins/example", "inactive", 1, symbol="EX")
    selected_folders = []
    created = []
    monkeypatch.setattr(archive, "_navigate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(archive, "_is_authenticated", lambda _page: True)
    monkeypatch.setattr(archive, "_open_coin_portfolio_picker", lambda *_args: "opened")
    monkeypatch.setattr(
        archive,
        "_portfolio_options",
        lambda _page: [{"number": 16, "href": "/en/portfolios/124912956", "label": "⭐ 16"}],
    )
    monkeypatch.setattr(archive, "_portfolio_asset_urls", lambda *_args: {f"/en/coins/coin-{n}" for n in range(1000)})
    monkeypatch.setattr(
        archive,
        "_create_portfolio",
        lambda _page, number: created.append(number)
        or {"number": number, "href": f"/en/portfolios/{number}", "label": str(number)},
    )
    monkeypatch.setattr(
        archive,
        "_click_portfolio_row_plus",
        lambda _page, number: selected_folders.append(number)
        or {"found": True, "clicked": True, "verified": True},
    )
    monkeypatch.setattr(archive, "_coin_detail_portfolio_state", lambda _page: "active")

    results = archive.add_coins_to_favorites(SimpleNamespace(), [coin])

    assert results[0]["status"] == "added"
    assert results[0]["portfolio_folder"] == 17
    assert created == [17]
    assert selected_folders == [17]


def test_add_coins_to_favorites_keeps_latest_portfolio_below_capacity(monkeypatch):
    coin = Coin("Example", "https://www.coingecko.com/en/coins/example", "inactive", 1, symbol="EX")
    selected_folders = []
    monkeypatch.setattr(archive, "_navigate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(archive, "_is_authenticated", lambda _page: True)
    monkeypatch.setattr(archive, "_open_coin_portfolio_picker", lambda *_args: "opened")
    monkeypatch.setattr(
        archive,
        "_portfolio_options",
        lambda _page: [{"number": 16, "href": "/en/portfolios/124912956", "label": "⭐ 16"}],
    )
    monkeypatch.setattr(archive, "_portfolio_asset_urls", lambda *_args: {f"/en/coins/coin-{n}" for n in range(999)})
    monkeypatch.setattr(
        archive,
        "_create_portfolio",
        lambda *_args: (_ for _ in ()).throw(AssertionError("portfolio 16 still has capacity")),
    )
    monkeypatch.setattr(
        archive,
        "_click_portfolio_row_plus",
        lambda _page, number: selected_folders.append(number)
        or {"found": True, "clicked": True, "verified": True},
    )
    monkeypatch.setattr(archive, "_coin_detail_portfolio_state", lambda _page: "active")

    results = archive.add_coins_to_favorites(SimpleNamespace(), [coin])

    assert results[0]["status"] == "added"
    assert results[0]["portfolio_folder"] == 16
    assert selected_folders == [16]


def test_add_coins_to_favorites_never_clicks_when_portfolio_state_is_uncertain(monkeypatch):
    coin = Coin("Unknown", "https://www.coingecko.com/en/coins/unknown", "unknown", 1)
    monkeypatch.setattr(archive, "_navigate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(archive, "_is_authenticated", lambda _page: True)
    monkeypatch.setattr(archive, "_open_coin_portfolio_picker", lambda *_args: "unknown")
    monkeypatch.setattr(
        archive,
        "_click_portfolio_row_plus",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not add an uncertain coin")),
    )

    results = archive.add_coins_to_favorites(SimpleNamespace(), [coin])

    assert results[0]["status"] == "portfolio_state_unconfirmed"


def test_add_coins_to_favorites_requires_folder_check_after_click(monkeypatch):
    coin = Coin("Example", "https://www.coingecko.com/en/coins/example", "inactive", 1)
    monkeypatch.setattr(archive, "_navigate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(archive, "_is_authenticated", lambda _page: True)
    monkeypatch.setattr(archive, "_open_coin_portfolio_picker", lambda *_args: "opened")
    monkeypatch.setattr(
        archive,
        "_portfolio_options",
        lambda _page: [{"number": 16, "href": "/en/portfolios/124912956", "label": "⭐ 16"}],
    )
    monkeypatch.setattr(archive, "_portfolio_asset_urls", lambda *_args: set())
    monkeypatch.setattr(
        archive,
        "_click_portfolio_row_plus",
        lambda *_args: {"found": True, "clicked": True},
    )

    results = archive.add_coins_to_favorites(SimpleNamespace(), [coin])

    assert results[0]["status"] == "verification_failed"


def test_portfolio_folder_option_index_matches_exact_numeric_label():
    labels = ["⭐ My Portfolio", "⭐ 2", "⭐ 6", "⭐ 16", "Portfolio 17"]

    assert archive._portfolio_folder_option_index(labels, 16) == 3
    assert archive._portfolio_folder_option_index(labels, 17) == 4
    assert archive._portfolio_folder_option_index(labels, 1) is None
    assert archive._portfolio_label_number("⭐ 16") == 16
    assert archive._portfolio_label_number("Portfolio 17") == 17


def test_click_portfolio_row_plus_targets_exact_numbered_detail_picker_row():
    clicked = []

    class Plus:
        def count(self):
            return 1

        def click(self, **_kwargs):
            clicked.append(self.folder_number)

        def __init__(self, folder_number):
            self.folder_number = folder_number

    class Row:
        def __init__(self, label, folder_number):
            self.label = label
            self.plus = Plus(folder_number)

        def inner_text(self):
            return self.label

        def locator(self, selector):
            if selector == "i.fa-plus":
                return self.plus
            assert selector == "i.fa-check"
            return Check()

    class Check:
        def wait_for(self, **kwargs):
            assert kwargs == {"state": "visible", "timeout": 10_000}

    class Rows:
        def __init__(self):
            self.rows = [Row("⭐ 15", 15), Row("⭐ 16", 16)]

        def count(self):
            return len(self.rows)

        def nth(self, index):
            return self.rows[index]

    class Dropdown:
        def locator(self, selector):
            assert selector == archive._PORTFOLIO_PICKER_ROW_SELECTOR
            return Rows()

    class Page:
        def wait_for_selector(self, selector, **kwargs):
            assert selector == archive._PORTFOLIO_PICKER_PLUS_SELECTOR
            assert kwargs["state"] == "visible"

        def wait_for_timeout(self, _milliseconds):
            pass

        def locator(self, selector):
            assert selector == archive._DETAIL_PORTFOLIO_PICKER_SELECTOR
            return Dropdown()

    result = archive._click_portfolio_row_plus(Page(), 16)

    assert result == {"found": True, "clicked": True, "verified": True}
    assert clicked == [16]


def test_create_portfolio_uses_detail_picker_new_portfolio_action(monkeypatch):
    state = {"clicked": False, "name": None, "submitted": False}
    expected_selector = (
        f'{archive._DETAIL_PORTFOLIO_PICKER_SELECTOR} '
        '[data-action*="coin-favorites#handleNewPortfolio"]'
    )

    class Submit:
        @property
        def first(self):
            return self

        def count(self):
            return 1

        def is_visible(self):
            return True

        def click(self, **_kwargs):
            state["submitted"] = True

    class Form:
        def locator(self, selector):
            assert selector == 'button[type="submit"], input[type="submit"]'
            return Submit()

    class NameInput:
        def is_visible(self):
            return True

        def get_attribute(self, name):
            return "name" if name == "name" else None

        def fill(self, value):
            state["name"] = value

        def locator(self, selector):
            assert selector == "xpath=ancestor::form"
            return Form()

    class Inputs:
        def count(self):
            return 1

        def nth(self, _index):
            return NameInput()

    class CreateOption:
        @property
        def first(self):
            return self

        def count(self):
            return 1

        def click(self, **_kwargs):
            state["clicked"] = True

    class Page:
        def locator(self, selector):
            if selector == expected_selector:
                return CreateOption()
            if selector == "input":
                return Inputs()
            raise AssertionError(f"Unexpected selector: {selector}")

        def wait_for_timeout(self, _milliseconds):
            pass

    monkeypatch.setattr(
        archive,
        "_portfolio_options",
        lambda _page: [{"number": 17, "href": "/en/portfolios/17", "label": "⭐ 17"}],
    )

    result = archive._create_portfolio(Page(), 17)

    assert state == {"clicked": True, "name": "17", "submitted": True}
    assert result == {"number": 17, "href": "/en/portfolios/17", "label": "⭐ 17"}


def test_portfolio_asset_urls_counts_unique_assets_across_pages(monkeypatch):
    class Link:
        def __init__(self, href):
            self.href = href

        def get_attribute(self, _name):
            return self.href

        def is_visible(self):
            return True

    class Row:
        def __init__(self, hrefs):
            self.hrefs = hrefs

        def locator(self, _selector):
            return Links(self.hrefs)

    class Links:
        def __init__(self, hrefs):
            self.hrefs = hrefs

        def count(self):
            return len(self.hrefs)

        def nth(self, index):
            return Link(self.hrefs[index])

        def is_visible(self):
            return True

    class FakePage:
        def __init__(self):
            self.page_number = 1
            self.url = "https://www.coingecko.com/en/portfolios/124912956"

        def locator(self, selector):
            if selector == "table tbody tr":
                hrefs = (
                    [["/en/coins/alpha"], ["/en/coins/beta"]]
                    if self.page_number == 1
                    else [["/en/coins/beta"], ["/en/coins/gamma"]]
                )
                return Rows([Row(row) for row in hrefs])
            if selector == 'a[aria-label="next"], a[rel="next"]':
                return Links(["/en/portfolios/124912956?page=2"] if self.page_number == 1 else [])
            raise AssertionError(f"Unexpected selector: {selector}")

    class Rows:
        def __init__(self, rows):
            self.rows = rows

        def count(self):
            return len(self.rows)

        def nth(self, index):
            return self.rows[index]

    page = FakePage()

    def fake_navigate(_page, url):
        page.page_number = 2 if "page=2" in url else 1
        page.url = url

    monkeypatch.setattr(archive, "_navigate", fake_navigate)

    assert archive._portfolio_asset_urls(page, "/en/portfolios/124912956") == {
        "/en/coins/alpha",
        "/en/coins/beta",
        "/en/coins/gamma",
    }


def test_parse_listing_html_reads_toggle_state_from_aria_label_and_data_action():
    html = """
    <table><tbody>
      <tr>
        <td><button aria-label="Remove from portfolio" data-action="coin-favorites#toggle" aria-pressed="true"></button></td>
        <td><a href="/en/coins/example">Example</a></td>
        <td>2 days</td>
      </tr>
    </tbody></table>
    """
    coins = parse_listing_html(html, 1)
    assert coins[0].portfolio == "active"


def test_auto_add_inactive_coins_promotes_to_active_before_capture(monkeypatch):
    coin = Coin("Example", "https://www.coingecko.com/en/coins/example", "inactive", 1, "1 hour", "EX")
    unknown = Coin("Unknown", "https://www.coingecko.com/en/coins/unknown", "unknown", 1, "1 hour", "UNK")

    monkeypatch.setattr(archive, "_is_authenticated", lambda page: True)
    monkeypatch.setattr(archive, "add_coins_to_favorites", lambda page, coins: [{
        "coin_url": coin.url if coin.url == item.url else unknown.url,
        "display_name": item.label(),
        "symbol": item.symbol,
        "status": "added",
    } for item in coins])

    refreshed, results = _auto_add_inactive_coins(SimpleNamespace(), [coin, unknown])
    assert refreshed[0].portfolio == "active"
    assert refreshed[1].portfolio == "active"
    assert results[0]["status"] == "added"


def test_auto_add_inactive_coins_reports_login_required_without_clicking(monkeypatch):
    coin = Coin("Example", "https://www.coingecko.com/en/coins/example", "inactive", 1, "1 hour", "EX")

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("must not click while signed out")

    monkeypatch.setattr(archive, "_is_authenticated", lambda _page: False)
    monkeypatch.setattr(archive, "add_coins_to_favorites", fail_if_called)

    refreshed, results = _auto_add_inactive_coins(SimpleNamespace(), [coin])

    assert refreshed == [coin]
    assert results == [
        {
            "coin_url": coin.url,
            "display_name": coin.label(),
            "symbol": coin.symbol,
            "status": "login_required",
        }
    ]


def test_devtools_active_port_parser_and_missing_file(tmp_path):
    assert read_devtools_active_port(tmp_path) is None
    path = tmp_path / "DevToolsActivePort"
    path.write_text("45678\n/devtools/browser/abc-def\n", encoding="utf-8")
    assert read_devtools_active_port(tmp_path) == "ws://127.0.0.1:45678/devtools/browser/abc-def"
    path.write_text("not-a-port\n/devtools/browser/abc\n", encoding="utf-8")
    assert read_devtools_active_port(tmp_path) is None


def test_browser_session_automatically_attaches_unless_profile_is_explicit(monkeypatch):
    endpoint = "ws://127.0.0.1:9222/devtools/browser/example"
    monkeypatch.setattr(archive, "chrome_debug_endpoint", lambda *_args, **_kwargs: endpoint)

    automatic = SimpleNamespace(
        browser="chrome",
        profile=None,
        attach=False,
        cdp_url=None,
        chrome_user_data=None,
    )
    dedicated = SimpleNamespace(
        browser="chrome",
        profile="C:/isolated/profile",
        attach=False,
        cdp_url=None,
        chrome_user_data=None,
    )
    explicit = SimpleNamespace(
        browser="chrome",
        profile=None,
        attach=False,
        cdp_url="ws://127.0.0.1:9223/devtools/browser/explicit",
        chrome_user_data=None,
    )

    assert archive._browser_session_endpoint(automatic) == endpoint
    assert archive._browser_session_endpoint(dedicated) is None
    assert archive._browser_session_endpoint(explicit) == explicit.cdp_url


def test_selection_json_accepts_strings_or_coin_objects(tmp_path):
    path = tmp_path / "selection.json"
    path.write_text(
        '{"urls": ["https://www.coingecko.com/en/coins/a", '
        '{"coin_url": "https://www.coingecko.com/en/coins/b/"}]}',
        encoding="utf-8",
    )
    assert _load_selection(str(path)) == {
        "https://www.coingecko.com/en/coins/a",
        "https://www.coingecko.com/en/coins/b",
    }
    assert _load_selection(None) is None


def test_empty_rendered_pdf_is_rejected_and_temporary_file_removed(tmp_path):
    class FakeSession:
        def send(self, method, options):
            return {"data": base64.b64encode(b"%PDF-1.7\n%%EOF").decode("ascii")}

        def detach(self):
            pass

    class FakeContext:
        def new_cdp_session(self, page):
            return FakeSession()

    class FakePage:
        context = FakeContext()

    target = tmp_path / "empty.pdf"
    try:
        _save_page_as_pdf(FakePage(), target)
    except ValueError as error:
        assert "empty or incomplete" in str(error)
    else:
        raise AssertionError("Empty PDFs must be rejected.")

    assert not target.exists()
    assert not target.with_name(target.stem + ".part" + target.suffix).exists()
