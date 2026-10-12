from pathlib import Path

from coingecko_archive import (
    Coin,
    deduplicate_coins,
    extract_labeled_link,
    is_complete,
    is_pdf_content,
    page_url,
    parse_listing_html,
    retry,
    sanitize_filename,
)


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
