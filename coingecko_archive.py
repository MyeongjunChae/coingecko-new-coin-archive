from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, TypeVar
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag
from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

LIST_URL = "https://www.coingecko.com/en/new-cryptocurrencies"
MAX_PDF_BYTES = 100 * 1024 * 1024
RETRIES = 3
PORTFOLIO_FOLDER_SIZE = 1000
T = TypeVar("T")


@dataclass(frozen=True)
class Coin:
    name: str
    url: str
    portfolio: str
    page: int
    added: str | None = None
    symbol: str = ""
    duplicate_index: int = 1

    @property
    def slug(self) -> str:
        return coin_slug(self.url)

    def label(self) -> str:
        """Human-readable, collision-resistant name for reports and the web UI."""
        name = " ".join(self.name.split()) or "Unknown"
        symbol = " ".join(self.symbol.split())
        if symbol and not symbol_in_name(name, symbol):
            base = f"{name} ({symbol})"
        else:
            base = name
        if self.duplicate_index > 1:
            base = f"{base} #{self.duplicate_index}"
        return f"{base} [{self.slug}]" if self.slug else base

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "symbol": self.symbol,
            "slug": self.slug,
            "display_name": self.label(),
            "coin_url": self.url,
            "listed_age": self.added,
            "portfolio_state": self.portfolio,
            "page": self.page,
            "duplicate_index": self.duplicate_index,
        }


class AccessWallError(RuntimeError):
    pass


def coin_slug(url: str) -> str:
    """Return the CoinGecko coin slug from a coin URL (or a composite URL)."""
    base = url.split("|", 1)[0]
    path = urlsplit(base).path.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else ""


def symbol_in_name(name: str, symbol: str) -> bool:
    """True when appending the symbol would be redundant.

    "Auren Protocol" contains the letters "AU" but still benefits from
    "Auren Protocol (AU)"; "WHALE", "BONER.com", and "Bitcoin BTC" do not.
    """
    normalized_name = " ".join(name.split()).casefold()
    normalized_symbol = " ".join(symbol.split()).casefold()
    if not normalized_symbol:
        return True
    if normalized_name == normalized_symbol:
        return True
    if f"({normalized_symbol})" in normalized_name:
        return True
    return normalized_name.endswith(" " + normalized_symbol)


def is_tokenized_stock_name(name: str, symbol: str = "") -> bool:
    """Identify tokenized securities that should be favorited without capture."""
    text = " ".join((name, symbol)).casefold()
    return "tokenized" in text and ("stock" in text or "share" in text or "token" in text)


def assign_duplicate_indices(coins: Iterable[Coin]) -> list[Coin]:
    """Add a visible #2, #3 suffix when name+symbol collide in one result set."""
    counts: dict[tuple[str, str], int] = {}
    result: list[Coin] = []
    for coin in coins:
        key = (coin.name.casefold().strip(), coin.symbol.casefold().strip())
        counts[key] = counts.get(key, 0) + 1
        result.append(replace(coin, duplicate_index=counts[key]))
    return result


def page_url(page_number: int, base_url: str = LIST_URL) -> str:
    parts = urlsplit(base_url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "page"]
    query.append(("page", str(page_number)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _control_identity(control: Tag) -> bool:
    labels = " ".join(
        str(control.get(attribute, ""))
        for attribute in ("aria-label", "title", "data-testid", "class", "data-action")
    ).lower()
    return any(
        word in labels
        for word in (
            "star",
            "favorite",
            "favourite",
            "watchlist",
            "portfolio",
            "coin-favorites",
            "bookmark",
        )
    )


def _control_state(control: Tag) -> str:
    values: list[str] = []
    for attribute in ("aria-pressed", "aria-checked", "data-state", "data-active", "data-favorited"):
        value = control.get(attribute)
        if value is not None:
            values.append(str(value).strip().lower())

    label = " ".join(
        str(control.get(attribute, ""))
        for attribute in ("aria-label", "title", "data-testid", "data-action")
    ).strip().lower()
    if re.search(
        r"\b(remove|unfollow|unfavorite|un-favorite|unstar|un-star|remove from portfolio|delete from portfolio)\b",
        label,
    ):
        values.append("active")
    elif re.search(r"\b(add|follow|add to portfolio|favorite|watchlist)\b", label):
        values.append("inactive")

    classes = {str(item).lower() for item in control.get("class", [])}
    if "fas" in classes or "fa-solid" in classes or "active" in classes:
        values.append("active")
    if "far" in classes or "fa-regular" in classes or "inactive" in classes:
        values.append("inactive")

    active_values = {"true", "checked", "selected", "on", "active", "1"}
    inactive_values = {"false", "unchecked", "off", "inactive", "0"}
    states = {
        "active" if value in active_values else "inactive"
        for value in values
        if value in active_values or value in inactive_values
    }
    if len(states) == 1:
        return next(iter(states))
    if "active" in values and "inactive" not in values:
        return "active"
    if "inactive" in values and "active" not in values:
        return "inactive"
    return "unknown"


def _listing_row(anchor: Tag) -> Tag | None:
    for parent in anchor.parents:
        if not isinstance(parent, Tag):
            continue
        if parent.name == "tr" or parent.get("role") == "row":
            return parent
        if parent.name in {"body", "html"}:
            break
    return None


def _portfolio_state(container: Tag) -> str:
    candidates: list[str] = []
    for control in container.select("button, [role='button'], [aria-pressed], [aria-checked]"):
        if _control_identity(control):
            candidates.append(_control_state(control))
    for icon in container.select("i[data-action*='coin-favorites'][class*='fa-star']"):
        event = str(icon.get("data-analytics-event", "")).lower()
        classes = set(icon.get("class", []))
        if "select_remove_portfolio_coin_cta" in event:
            candidates.append("active")
        elif "select_add_portfolio_coin_cta" in event:
            candidates.append("inactive")
        elif "fas" in classes or "fa-solid" in classes:
            candidates.append("active")
        elif "far" in classes or "fa-regular" in classes:
            candidates.append("inactive")
        else:
            candidates.append("unknown")
    states = set(candidates)
    if not candidates or "unknown" in states or len(states) != 1:
        return "unknown"
    return candidates[0]


def _row_age(row: Tag) -> str | None:
    cells = row.find_all(["td", "th"], recursive=False)
    if not cells:
        return None
    return " ".join(cells[-1].get_text(" ", strip=True).split()) or None


def parse_listing_html(html: str, page_number: int, base_url: str = LIST_URL) -> list[Coin]:
    soup = BeautifulSoup(html, "html.parser")
    coins: list[Coin] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = urljoin(base_url, str(anchor["href"]))
        parts = urlsplit(href)
        if not re.search(r"/coins/[^/]+/?$", parts.path):
            continue
        row = _listing_row(anchor)
        if row is None:
            continue
        canonical = urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), "", ""))
        if canonical in seen:
            continue
        full_name = " ".join(anchor.get_text(" ", strip=True).split())
        if not full_name:
            full_name = str(anchor.get("aria-label", "")).strip()
        if not full_name:
            continue

        # CoinGecko renders "Name SYMBOL" inside the link. The image alt and
        # the last nested div both carry the symbol; strip the trailing symbol
        # from the visible text so the CSV does not say "WHALE WHALE".
        symbol = ""
        image = anchor.find("img", alt=True)
        if image:
            symbol = " ".join(str(image.get("alt", "")).split())
        if not symbol:
            leaves = [
                node
                for node in anchor.find_all("div")
                if node.find("div") is None and node.get_text(" ", strip=True)
            ]
            if leaves:
                candidate = " ".join(leaves[-1].get_text(" ", strip=True).split())
                if candidate.casefold() != full_name.casefold():
                    symbol = candidate
        name = full_name
        if symbol and name.casefold().endswith(symbol.casefold()):
            trimmed = name[: -len(symbol)].strip()
            if trimmed:
                name = trimmed

        seen.add(canonical)
        coins.append(
            Coin(
                name,
                canonical,
                _portfolio_state(row),
                page_number,
                _row_age(row),
                symbol,
            )
        )
    return coins


def deduplicate_coins(coins: Iterable[Coin]) -> list[Coin]:
    unique: dict[str, Coin] = {}
    conflicting: set[str] = set()
    for coin in coins:
        key = coin.url.rstrip("/").lower()
        previous = unique.get(key)
        if previous is None:
            unique[key] = coin
        elif previous.portfolio != coin.portfolio:
            conflicting.add(key)
    for key in conflicting:
        coin = unique[key]
        unique[key] = replace(coin, portfolio="unknown")
    return list(unique.values())


def _parse_age(value: str | None, now: datetime | None = None) -> timedelta | None:
    if not value:
        return None
    normalized = re.sub(r"\babout\b", "", value.casefold()).strip()
    if normalized in {"just now", "less than a minute", "a few seconds"}:
        age = timedelta(0)
    else:
        match = re.fullmatch(r"(\d+|a|an)\s+(minute|minutes|hour|hours|day|days|week|weeks)", normalized)
        if match:
            amount = 1 if match.group(1) in {"a", "an"} else int(match.group(1))
            unit = match.group(2).rstrip("s")
            multiplier = {
                "minute": 1,
                "hour": 60,
                "day": 1440,
                "week": 10080,
            }[unit]
            age = timedelta(minutes=amount * multiplier)
        else:
            parsed: datetime | None = None
            for date_format in ("%b %d, %Y", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(value.strip(), date_format)
                    break
                except ValueError:
                    continue
            if parsed is None:
                try:
                    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
                except ValueError:
                    return None
            current = now or datetime.now(parsed.tzinfo)
            if current.tzinfo and parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=current.tzinfo)
            age = current - parsed
    return age if age >= timedelta(0) else None


def age_within_days(value: str | None, days: int, now: datetime | None = None) -> bool:
    age = _parse_age(value, now)
    return age is not None and age <= timedelta(days=days)


def _recent_sort_key(coin: Coin) -> tuple[bool, timedelta]:
    age = _parse_age(coin.added)
    return age is None, age or timedelta.max


def extract_labeled_link(html: str, labels: set[str], base_url: str) -> tuple[str | None, str]:
    soup = BeautifulSoup(html, "html.parser")
    found: set[str] = set()
    normalized_labels = {label.casefold() for label in labels}
    for anchor in soup.select("a[href]"):
        text = " ".join(anchor.get_text(" ", strip=True).split()).casefold()
        accessible = " ".join(
            str(anchor.get(attribute, "")) for attribute in ("aria-label", "title")
        ).strip().casefold()
        if text in normalized_labels or accessible in normalized_labels:
            href = urljoin(base_url, str(anchor["href"]))
            if urlsplit(href).scheme in {"http", "https"}:
                found.add(href)
    if len(found) == 1:
        return next(iter(found)), "ok"
    return None, "missing" if not found else "ambiguous"


def _external_url(href: str, base_url: str) -> str | None:
    value = urljoin(base_url, href)
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        return None
    if (parts.hostname or "").lower().endswith("coingecko.com"):
        return None
    return value


def _labeled_link_row_targets(html: str, labels: set[str], base_url: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    normalized_labels = {label.casefold() for label in labels}
    found: set[tuple[str, str]] = set()
    for row in soup.find_all(["div", "li", "tr"]):
        children = row.find_all(recursive=False)
        if len(children) < 2:
            continue
        row_label = " ".join(children[0].get_text(" ", strip=True).split()).casefold()
        if row_label not in normalized_labels:
            continue
        for anchor in children[1].select("a[href]"):
            href = _external_url(str(anchor["href"]), base_url)
            if href:
                text = " ".join(anchor.get_text(" ", strip=True).split()).casefold()
                found.add((href, text))
    return sorted(found)


def extract_coin_website(html: str, base_url: str) -> tuple[str | None, str]:
    links = [
        href
        for href, text in _labeled_link_row_targets(html, {"website"}, base_url)
        if text not in {"whitepaper", "white paper"}
    ]
    unique = set(links)
    if len(unique) == 1:
        return next(iter(unique)), "ok"
    if len(unique) > 1:
        return None, "ambiguous"
    return extract_labeled_link(html, {"website", "official website"}, base_url)


def extract_twitter_link(html: str, base_url: str) -> tuple[str | None, str]:
    community_links = _labeled_link_row_targets(html, {"community"}, base_url)
    if community_links:
        candidates = [(href, text) for href, text in community_links if text in {"twitter", "x"}]
    else:
        candidates = [
            (
                urljoin(base_url, str(anchor["href"])),
                " ".join(anchor.get_text(" ", strip=True).split()).casefold(),
            )
            for anchor in BeautifulSoup(html, "html.parser").select("a[href]")
            if " ".join(anchor.get_text(" ", strip=True).split()).casefold() in {"twitter", "x"}
        ]
    found: set[str] = set()
    for raw_href, _ in candidates:
        href = _external_url(raw_href, base_url)
        if not href:
            continue
        host = (urlsplit(href).hostname or "").lower()
        if host not in {"twitter.com", "www.twitter.com", "x.com", "www.x.com"}:
            continue
        path_parts = [part for part in urlsplit(href).path.split("/") if part]
        if not path_parts or path_parts[0].lower() in {
            "intent", "share", "search", "home", "explore", "login", "i"
        }:
            continue
        found.add(urlunsplit((urlsplit(href).scheme, urlsplit(href).netloc, f"/{path_parts[0]}", "", "")))
    if len(found) == 1:
        return next(iter(found)), "ok"
    return None, "missing" if not found else "ambiguous"


def extract_explorer_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    labeled = _labeled_link_row_targets(html, {"explorer", "explorers"}, base_url)
    found: set[str] = set()
    if labeled:
        for href, _ in labeled:
            parts = urlsplit(href)
            canonical = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
            existing = next((item for item in found if item.split("#", 1)[0] == canonical), None)
            if existing is None or (not urlsplit(existing).fragment and parts.fragment):
                if existing:
                    found.remove(existing)
                found.add(href)
        return sorted(found)
    explorer_hosts = (
        "explorer", "etherscan", "solscan", "basescan", "arbiscan",
        "polygonscan", "bscscan", "tronscan", "tonviewer", "blockscout",
        "snowtrace", "ftmscan", "optimistic.etherscan",
    )
    for anchor in soup.select("a[href]"):
        href = _external_url(str(anchor["href"]), base_url)
        if not href:
            continue
        text = " ".join(anchor.get_text(" ", strip=True).split()).casefold()
        accessible = " ".join(
            str(anchor.get(attribute, "")) for attribute in ("aria-label", "title")
        ).strip().casefold()
        host = (urlsplit(href).hostname or "").casefold()
        if (
            "explorer" in text
            or "explorer" in accessible
            or any(marker in host for marker in explorer_hosts)
        ):
            found.add(href)
    return sorted(found)


def _twitter_account_unavailable(page: Page) -> bool:
    text = page.locator("body").inner_text(timeout=5000).casefold()
    normalized = text.replace("’", "'").replace("‘", "'")
    return any(
        marker in normalized
        for marker in (
            "this account doesn't exist",
            "this account has been suspended",
            "account suspended",
            "account is suspended",
            "this page doesn't exist",
            "account not found",
            "user not found",
            "this account is unavailable",
        )
    )


def sanitize_filename(name: str, limit: int = 100) -> str:
    value = unicodedata.normalize("NFC", name)
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = value[:limit].rstrip(" .")
    if not value or value.upper() in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }:
        return "coin"
    return value


def coin_key(url: str) -> str:
    return hashlib.sha256(url.rstrip("/").lower().encode("utf-8")).hexdigest()[:10]


def output_path(
    directory: Path,
    name: str,
    url: str,
    suffix: str,
    symbol: str = "",
    duplicate_index: int = 1,
) -> Path:
    """Build a stable, human-readable filename.

    The slug keeps same-named coins visibly distinct; the short URL hash keeps
    target pages (website/twitter/explorer) for one coin distinct as well.
    Duplicate entries get a human-visible #N suffix so downloaded files stay
    easy to distinguish when names collide.
    """
    directory.mkdir(parents=True, exist_ok=True)
    label = name
    if symbol and not symbol_in_name(name, symbol):
        label = f"{name} ({symbol})"
    if duplicate_index > 1:
        label = f"{label} #{duplicate_index}"
    slug = sanitize_filename(coin_slug(url), limit=40)
    label = sanitize_filename(label, limit=60)
    parts = [part for part in (label, slug, coin_key(url)) if part]
    return directory / f"{'-'.join(parts)}{suffix}"


def is_pdf_content(prefix: bytes) -> bool:
    return prefix.startswith(b"%PDF-")


def is_complete(record: dict) -> bool:
    return record.get("status") == "completed"


def _capture_allowed(coin: Coin, include_unstarred: bool, auto_added: bool = False) -> bool:
    """Capture only newly added coins by default; Tokenized Stock entries are add-only."""
    if is_tokenized_stock_name(coin.name, coin.symbol):
        return False
    return include_unstarred or auto_added


def _auto_add_inactive_coins(page: Page, selected: list[Coin]) -> tuple[list[Coin], list[dict]]:
    """Ensure selected coins are in the active portfolio before capture.

    A one-click collection should both protect the target page and add the coin to
    the active portfolio so capture and favorites are aligned in a single step.
    """
    if not selected:
        return selected, []
    queue = [coin for coin in selected if coin.portfolio != "active"]
    if not queue:
        return selected, []
    if not _is_authenticated(page):
        return selected, [
            {
                "coin_url": coin.url,
                "display_name": coin.label(),
                "symbol": coin.symbol,
                "status": "login_required",
            }
            for coin in queue
        ]
    results = add_coins_to_favorites(page, queue)
    by_url = {
        str(item["coin_url"]).rstrip("/").lower(): item
        for item in results
        if isinstance(item, dict) and item.get("coin_url")
    }
    refreshed: list[Coin] = []
    for coin in selected:
        key = coin.url.rstrip("/").lower()
        status = by_url.get(key, {}).get("status")
        if status in {"added", "already_active"}:
            refreshed.append(replace(coin, portfolio="active"))
        else:
            refreshed.append(replace(coin, portfolio="unknown" if coin.portfolio == "unknown" else coin.portfolio))
    return refreshed, results


def retry(operation: Callable[[], T], attempts: int = RETRIES, delay: float = 1.0) -> T:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return operation()
        except AccessWallError:
            raise
        except Exception as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(delay * (2**attempt))
    assert last_error is not None
    raise last_error


def _load_manifest(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "coins": {}, "pages": []}
    with path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("version") != 1 or not isinstance(manifest.get("coins"), dict):
        raise ValueError(f"Unsupported or invalid manifest: {path}")
    return manifest


def _save_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _save_capture_report(path: Path, results: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    failures = [record for record in results if record.get("review")]
    lines = [
        f"CoinGecko capture report — {datetime.now().astimezone().isoformat()}",
        f"Processed: {len(results)}; with review items: {len(failures)}",
        "",
    ]
    for record in results:
        name = " ".join(
            str(record.get("display_name") or record.get("name", "Unknown")).split()
        )
        lines.append(f"[{record.get('status', 'unknown')}] {name}")
        for key in ("screenshot", "twitter_screenshot", "whitepaper_screenshot", "pdf"):
            if record.get(key):
                lines.append(f"  {key}: {record[key]}")
        for item in record.get("explorer_screenshots", []):
            lines.append(f"  explorer_screenshot: {item}")
        for reason in record.get("review", []):
            lines.append(f"  REVIEW: {reason}")
    try:
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _save_preview(path: Path, coins: Iterable[Coin]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=(
                    "display_name",
                    "symbol",
                    "slug",
                    "name",
                    "coin_url",
                    "listed_age",
                    "portfolio_state",
                    "page",
                    "duplicate_index",
                ),
            )
            writer.writeheader()
            for coin in coins:
                writer.writerow(
                    {
                        "display_name": coin.label(),
                        "symbol": coin.symbol,
                        "slug": coin.slug,
                        "name": coin.name,
                        "coin_url": coin.url,
                        "listed_age": coin.added or "",
                        "portfolio_state": coin.portfolio,
                        "page": coin.page,
                        "duplicate_index": coin.duplicate_index,
                    }
                )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _check_access_wall(page: Page) -> None:
    body = page.locator("body").inner_text(timeout=5000).casefold()
    markers = (
        "verify you are human",
        "checking your browser",
        "just a moment",
        "captcha",
        "access denied",
        "unusual traffic",
        "checking if the site connection is secure",
    )
    challenge_frame = page.locator("iframe[src*='challenges.cloudflare.com']").count() > 0
    if challenge_frame or any(marker in body for marker in markers):
        raise AccessWallError("An anti-bot or access challenge was detected; stopping without bypassing it.")


def _is_authenticated(page: Page) -> bool:
    """CoinGecko shows a sign-in modal button and "Log in"/"Sign up" text when signed out."""
    try:
        if page.locator("button[data-action*='openSignInModal']").first.is_visible(
            timeout=2000
        ):
            return False
    except PlaywrightError:
        pass
    try:
        body = page.locator("body").inner_text(timeout=5000)
    except PlaywrightError:
        return False
    lines = {line.strip().casefold() for line in body.splitlines() if line.strip()}
    signed_out_labels = {
        "log in",
        "login",
        "sign up",
        "create an account",
        "로그인",
        "회원가입",
    }
    return not (signed_out_labels & lines)


def _navigate(page: Page, url: str) -> Response | None:
    response = retry(lambda: page.goto(url, wait_until="domcontentloaded", timeout=45_000))
    _check_access_wall(page)
    if response and response.status in {401, 403, 429}:
        raise AccessWallError(f"HTTP {response.status} while opening {url}; stopping without bypass.")
    page.wait_for_timeout(1800)
    return response


def _listing_page(page: Page, number: int) -> list[Coin]:
    url = page_url(number)
    _navigate(page, url)
    try:
        page.wait_for_selector("a[href*='/coins/']", timeout=15_000)
    except PlaywrightTimeoutError:
        _check_access_wall(page)
    coins = parse_listing_html(page.content(), number, url)
    if not _is_authenticated(page):
        return [replace(coin, portfolio="unknown") for coin in coins]
    return coins


_COIN_DETAIL_PORTFOLIO_STATE_JS = r"""
() => {
  const visible = (element) => {
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0
      && getComputedStyle(element).visibility !== 'hidden';
  };
  const buttons = [...document.querySelectorAll(
    'button[data-action*="coin-favorites#handleCoinFavourites"]'
  )].filter(visible);
  const button = buttons.find((element) => {
    const properties = element.getAttribute('data-analytics-event-properties') || '';
    return properties.includes('"sub_origin":"primary_cta"');
  }) || buttons[0];
  if (!button) return 'unknown';

  const event = (button.getAttribute('data-analytics-event') || '').toLowerCase();
  const label = `${button.getAttribute('aria-label') || ''} ${button.innerText || ''}`.toLowerCase();
  const star = button.querySelector('.fa-star, .fas, .far, .fa-solid, .fa-regular');
  const classes = Array.from(star?.classList || []).map((name) => name.toLowerCase());
  const controller = button.closest('[data-controller~="coin-favorites"]');
  const dropdown = controller?.querySelector('[data-coin-favorites-target="dropdown"]');
  const rows = [...(dropdown?.querySelectorAll(
    'div[data-coin-favorites-target="dropdownItem"]'
  ) || [])];
  if (rows.some((row) => row.querySelector('i.fa-check, i.fa-check-circle'))) {
    return 'active';
  }
  if (event.includes('remove_portfolio')
      || label.includes('remove from portfolio')
      || classes.includes('fas')
      || classes.includes('fa-solid')) return 'active';
  if (event.includes('select_add_portfolio_coin_cta')
      && (classes.includes('far') || classes.includes('fa-regular'))) return 'inactive';
  return 'unknown';
}
"""

_DETAIL_PORTFOLIO_PICKER_SELECTOR = (
    '[data-controller~="coin-favorites"]:has('
    'button[data-coin-show-target="portfolioButton"]) '
    '[data-coin-favorites-target="dropdown"]'
)
_PORTFOLIO_PICKER_ROW_SELECTOR = (
    'div[data-coin-favorites-target="dropdownItem"]'
    '[data-action*="click->coin-favorites#handle"]'
)
_PORTFOLIO_PICKER_PLUS_SELECTOR = (
    f"{_DETAIL_PORTFOLIO_PICKER_SELECTOR} "
    f"{_PORTFOLIO_PICKER_ROW_SELECTOR} i.fa-plus"
)
_NEW_PORTFOLIO_OPTION_SELECTOR = (
    f'{_DETAIL_PORTFOLIO_PICKER_SELECTOR} '
    '[data-action*="coin-favorites#handleNewPortfolio"]'
)


def _portfolio_label_number(label: str) -> int | None:
    normalized = re.sub(r"[⭐\ufe0f]", "", " ".join(label.split())).strip().casefold()
    match = re.fullmatch(r"(?:portfolio\s+|folder\s+|#)?(\d+)", normalized)
    return int(match.group(1)) if match else None


def _portfolio_folder_option_index(labels: Iterable[str], folder_number: int) -> int | None:
    """Find an exact numbered CoinGecko portfolio label."""
    for index, label in enumerate(labels):
        if _portfolio_label_number(label) == folder_number:
            return index
    return None


def _portfolio_options(page: Page) -> list[dict[str, int | str]]:
    links = page.locator('a[data-portfolio-type="coin"]')
    options: list[dict[str, int | str]] = []
    for index, label in enumerate(links.all_inner_texts()):
        number = _portfolio_label_number(label)
        href = links.nth(index).get_attribute("href")
        if number is not None and href:
            options.append({"number": number, "href": href, "label": label.strip()})
    return options


def _portfolio_asset_urls(page: Page, portfolio_href: str) -> set[str]:
    next_url = urljoin("https://www.coingecko.com", portfolio_href)
    expected_path = urlsplit(next_url).path.rstrip("/")
    visited: set[str] = set()
    assets: set[str] = set()
    for _ in range(100):
        if next_url in visited:
            break
        visited.add(next_url)
        _navigate(page, next_url)
        if urlsplit(page.url).path.rstrip("/") != expected_path:
            raise RuntimeError(f"Portfolio page did not load: {next_url}")
        rows = page.locator("table tbody tr")
        for row_index in range(rows.count()):
            coin_links = rows.nth(row_index).locator('a[href*="/coins/"]')
            for link_index in range(coin_links.count()):
                href = coin_links.nth(link_index).get_attribute("href")
                if href:
                    parts = urlsplit(urljoin(next_url, href))
                    if re.fullmatch(r"/en/coins/[^/]+", parts.path):
                        assets.add(parts.path.rstrip("/"))

        next_links = page.locator('a[aria-label="next"], a[rel="next"]')
        next_href = None
        for link_index in range(next_links.count()):
            link = next_links.nth(link_index)
            if link.is_visible():
                candidate = link.get_attribute("href")
                if candidate:
                    next_href = urljoin(next_url, candidate)
                    break
        if not next_href:
            break
        next_url = next_href
    return assets


def _open_coin_portfolio_picker(page: Page, coin: Coin) -> str:
    try:
        _navigate(page, coin.url)
        state = page.evaluate(_COIN_DETAIL_PORTFOLIO_STATE_JS)
        if state != "inactive":
            return state
        button = page.locator(
            'button[data-action*="coin-favorites#handleCoinFavourites"]'
            '[data-analytics-event="select_add_portfolio_coin_cta"]'
            '[data-analytics-event-properties*="primary_cta"]'
        )
        button.click(timeout=15_000)
        page.wait_for_selector(
            _NEW_PORTFOLIO_OPTION_SELECTOR,
            state="attached",
            timeout=15_000,
        )
        state = _coin_detail_portfolio_state(page)
        if state == "active":
            return "active"
        return "opened" if state == "inactive" else "unknown"
    except PlaywrightTimeoutError:
        return "picker_timeout"


def _click_portfolio_row_plus(page: Page, folder_number: int) -> dict:
    page.wait_for_selector(
        _PORTFOLIO_PICKER_PLUS_SELECTOR,
        state="visible",
        timeout=15_000,
    )
    page.wait_for_timeout(200)
    dropdown = page.locator(_DETAIL_PORTFOLIO_PICKER_SELECTOR)
    rows = dropdown.locator(_PORTFOLIO_PICKER_ROW_SELECTOR)
    matches = [
        rows.nth(index)
        for index in range(rows.count())
        if _portfolio_label_number(rows.nth(index).inner_text()) == folder_number
    ]
    if not matches:
        return {"found": False, "reason": "portfolio_row_not_found"}
    if len(matches) > 1:
        return {"found": False, "reason": "portfolio_row_ambiguous"}

    plus = matches[0].locator("i.fa-plus")
    if plus.count() != 1:
        return {"found": False, "reason": "portfolio_row_plus_not_found"}
    plus.click(timeout=10_000)
    matches[0].locator("i.fa-check").wait_for(state="visible", timeout=10_000)
    return {"found": True, "clicked": True, "verified": True}


def _create_portfolio(page: Page, folder_number: int) -> dict[str, int | str] | None:
    create_link = page.locator(
        _NEW_PORTFOLIO_OPTION_SELECTOR
    ).first
    if not create_link.count():
        return None
    create_link.click(timeout=10_000)
    page.wait_for_timeout(200)

    name_input = None
    inputs = page.locator("input")
    for index in range(inputs.count()):
        candidate = inputs.nth(index)
        if not candidate.is_visible():
            continue
        identifier = " ".join(
            filter(
                None,
                (
                    candidate.get_attribute("name"),
                    candidate.get_attribute("placeholder"),
                    candidate.get_attribute("aria-label"),
                ),
            )
        ).casefold()
        if "name" in identifier or "portfolio" in identifier:
            name_input = candidate
            break
    if name_input is None:
        return None
    name_input.fill(str(folder_number))

    form = name_input.locator("xpath=ancestor::form")
    submit = form.locator('button[type="submit"], input[type="submit"]')
    if submit.count() and submit.first.is_visible():
        submit.first.click(timeout=10_000)
    else:
        buttons = page.get_by_role("button", name=re.compile(r"create|save", re.IGNORECASE))
        visible_buttons = [
            buttons.nth(index)
            for index in range(buttons.count())
            if buttons.nth(index).is_visible()
        ]
        if not visible_buttons:
            return None
        visible_buttons[-1].click(timeout=10_000)

    page.wait_for_timeout(500)
    for option in _portfolio_options(page):
        if option["number"] == folder_number:
            return option
    return None


def _coin_detail_portfolio_state(page: Page) -> str:
    return page.evaluate(_COIN_DETAIL_PORTFOLIO_STATE_JS)


def add_coins_to_favorites(page: Page, coins: Iterable[Coin]) -> list[dict]:
    """Add unstarred coins to the newest portfolio, verifying capacity and success."""
    results: list[dict] = []
    seen_urls: set[str] = set()
    latest_folder: dict[str, int | str] | None = None
    folder_count = 0
    try:
        authenticated = _is_authenticated(page)
    except Exception as error:
        authenticated = False
        auth_error = str(error)
    else:
        auth_error = ""

    for coin in coins:
        key = coin.url.rstrip("/").lower()
        record = {
            "coin_url": coin.url,
            "display_name": coin.label(),
            "symbol": coin.symbol,
        }
        if key in seen_urls:
            record["status"] = "duplicate_input"
            results.append(record)
            continue
        seen_urls.add(key)
        if not authenticated:
            record["status"] = "login_required"
            if auth_error:
                record["error"] = auth_error
            results.append(record)
            continue
        if coin.portfolio == "active":
            record["status"] = "already_active"
            results.append(record)
            continue

        try:
            picker_state = _open_coin_portfolio_picker(page, coin)
            if picker_state == "active":
                record["status"] = "already_active"
                results.append(record)
                continue
            if picker_state == "unknown":
                record["status"] = "portfolio_state_unconfirmed"
                results.append(record)
                continue
            if picker_state == "picker_timeout":
                record["status"] = "portfolio_picker_timeout"
                results.append(record)
                continue

            if latest_folder is None:
                options = _portfolio_options(page)
                if not options:
                    record["status"] = "portfolio_folder_not_found"
                    results.append(record)
                    continue
                latest_folder = max(options, key=lambda option: int(option["number"]))
                assets = _portfolio_asset_urls(page, str(latest_folder["href"]))
                folder_count = len(assets)
                picker_state = _open_coin_portfolio_picker(page, coin)
                if picker_state != "opened":
                    record["status"] = (
                        "already_active"
                        if picker_state == "active"
                        else "portfolio_picker_timeout"
                        if picker_state == "picker_timeout"
                        else "portfolio_state_unconfirmed"
                    )
                    results.append(record)
                    continue

            if folder_count >= PORTFOLIO_FOLDER_SIZE:
                next_number = int(latest_folder["number"]) + 1
                created = _create_portfolio(page, next_number)
                if created is None:
                    record["status"] = "portfolio_creation_failed"
                    results.append(record)
                    continue
                latest_folder = created
                folder_count = 0
                picker_state = _open_coin_portfolio_picker(page, coin)
                if picker_state == "active":
                    record["portfolio_folder"] = next_number
                    record["status"] = "added"
                    folder_count += 1
                    results.append(record)
                    continue
                if picker_state != "opened":
                    record["status"] = (
                        "portfolio_picker_timeout"
                        if picker_state == "picker_timeout"
                        else "portfolio_state_unconfirmed"
                    )
                    results.append(record)
                    continue

            folder_number = int(latest_folder["number"])
            record["portfolio_folder"] = folder_number
            add_outcome = _click_portfolio_row_plus(page, folder_number)
            if not add_outcome.get("found") or not add_outcome.get("clicked"):
                record["status"] = add_outcome.get("reason", "portfolio_add_control_not_found")
                results.append(record)
                continue
            if not add_outcome.get("verified"):
                record["status"] = "verification_failed"
                results.append(record)
                continue
            record["status"] = "added"
            folder_count += 1
        except PlaywrightTimeoutError:
            record["status"] = (
                "login_required" if not _is_authenticated(page) else "verification_failed"
            )
        except Exception as error:
            record["status"] = f"failed:{type(error).__name__}"
            record["error"] = str(error)
            print(
                f"즐겨찾기 처리 실패: {coin.label()} "
                f"({type(error).__name__}: {error})"
            )
        results.append(record)
        print(f"즐겨찾기 처리 결과: {record['status']} - {coin.label()}")
    return results


def _compress_pdf_if_needed(path: Path, threshold_bytes: int = 2 * 1024 * 1024) -> None:
    """Reduce large PDFs with a best-effort external optimizer when available."""
    if not path.exists():
        return
    if path.stat().st_size < threshold_bytes:
        return
    executable = shutil.which("magick") or shutil.which("convert")
    if not executable:
        return
    compressed = path.with_name(f"{path.stem}.compressed{path.suffix}")
    command = [
        executable,
        "-quiet",
        "-density",
        "150",
        str(path),
        "-quality",
        "75",
        "-compress",
        "Zip",
        "-define",
        "pdf:use-cropbox=true",
        str(compressed),
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, ValueError):
        return
    if result.returncode != 0 or not compressed.exists():
        return
    if compressed.stat().st_size >= path.stat().st_size:
        compressed.unlink(missing_ok=True)
        return
    os.replace(compressed, path)


def _download_pdf(url: str, target: Path) -> tuple[bool, str]:
    temporary = target.with_suffix(target.suffix + ".part")
    try:
        with requests.get(url, stream=True, timeout=(15, 45), allow_redirects=True) as response:
            if response.status_code in {401, 403, 429}:
                raise AccessWallError(
                    f"HTTP {response.status_code} while downloading whitepaper; refusing to retry or bypass."
                )
            response.raise_for_status()
            if urlsplit(response.url).scheme not in {"http", "https"}:
                return False, "invalid_redirect"
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            iterator = response.iter_content(chunk_size=64 * 1024)
            first = next(iterator, b"")
            if not is_pdf_content(first):
                return False, f"not_pdf:{content_type or 'unknown-content-type'}"
            total = 0
            with temporary.open("wb") as handle:
                total += len(first)
                handle.write(first)
                for chunk in iterator:
                    total += len(chunk)
                    if total > MAX_PDF_BYTES:
                        raise ValueError(f"PDF exceeds the {MAX_PDF_BYTES // (1024 * 1024)} MB safety limit")
                    handle.write(chunk)
            os.replace(temporary, target)
            _compress_pdf_if_needed(target)
            return True, f"pdf:{content_type or 'unknown-content-type'}"
    finally:
        temporary.unlink(missing_ok=True)


def _browser_executable(browser: str) -> str | None:
    if browser == "chrome":
        executable_path = Path("Google") / "Chrome" / "Application" / "chrome.exe"
    elif browser == "edge":
        executable_path = Path("Microsoft") / "Edge" / "Application" / "msedge.exe"
    else:
        raise ValueError(f"Unsupported browser: {browser}")
    environments = ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
    if browser == "edge":
        environments = ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")
    for environment in environments:
        root = os.environ.get(environment)
        if root:
            candidate = Path(root) / executable_path
            if candidate.exists():
                return str(candidate)
    return None


def browser_user_data_dir(browser: str) -> Path:
    """Default User Data directory for the selected Chromium browser."""
    local = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    if browser == "edge":
        return local / "Microsoft" / "Edge" / "User Data"
    return local / "Google" / "Chrome" / "User Data"


def read_devtools_active_port(user_data_dir: Path) -> str | None:
    """Read Chrome's DevToolsActivePort file and return a CDP WebSocket URL.

    Chrome 136+ writes this file when the user explicitly enables
    "Allow remote debugging for this browser instance" at
    chrome://inspect/#remote-debugging. The format is:
        <port>
        /devtools/browser/<uuid>
    """
    path = user_data_dir / "DevToolsActivePort"
    try:
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        ]
    except OSError:
        return None
    if len(lines) < 2:
        return None
    try:
        port = int(lines[0])
    except ValueError:
        return None
    if not 1 <= port <= 65535:
        return None
    ws_path = lines[1]
    if not ws_path.startswith("/"):
        ws_path = "/" + ws_path
    return f"ws://127.0.0.1:{port}{ws_path}"


def chrome_debug_endpoint(browser: str = "chrome", user_data_dir: str | None = None) -> str | None:
    directory = Path(user_data_dir).expanduser() if user_data_dir else browser_user_data_dir(browser)
    return read_devtools_active_port(directory)


def _browser_session_endpoint(args: argparse.Namespace) -> str | None:
    """Prefer the live browser unless a dedicated profile was explicitly selected."""
    explicit_endpoint = getattr(args, "cdp_url", None)
    if explicit_endpoint:
        return explicit_endpoint
    if getattr(args, "attach", False) or not getattr(args, "profile", None):
        return chrome_debug_endpoint(
            args.browser, getattr(args, "chrome_user_data", None)
        )
    return None


def _open_browser_session(
    playwright, args: argparse.Namespace
) -> tuple[object | None, BrowserContext, Page, bool]:
    """Return (browser, context, page, owns_context).

    The live browser is preferred when its DevToolsActivePort file is available.
    An explicit ``--profile`` selects the isolated persistent-profile flow.
    The caller must close only the page, never an attached context/browser.
    """
    endpoint = _browser_session_endpoint(args)
    if endpoint:
        print("실사용 브라우저의 원격 디버깅 세션에 연결합니다.")
        browser = playwright.chromium.connect_over_cdp(endpoint, timeout=90_000)
        contexts = browser.contexts or [browser.new_context()]
        # Chrome can expose one context per profile. Prefer the profile that
        # already has a CoinGecko tab open, then fall back to the first one.
        context = contexts[0]
        for candidate in contexts:
            try:
                if any("coingecko.com" in (item.url or "") for item in candidate.pages):
                    context = candidate
                    break
            except PlaywrightError:
                continue
        page = context.new_page()
        return browser, context, page, False
    if getattr(args, "attach", False) or getattr(args, "cdp_url", None):
        endpoint = getattr(args, "cdp_url", None) or chrome_debug_endpoint(
            args.browser, getattr(args, "chrome_user_data", None)
        )
        if not endpoint:
            raise RuntimeError(
                "실사용 브라우저의 원격 디버깅이 꺼져 있습니다. "
                "주소창에 chrome://inspect/#remote-debugging 을 열고 "
                "'Allow remote debugging for this browser instance'를 켠 뒤 다시 실행하세요."
            )

    profile = (
        Path(args.profile).expanduser().resolve()
        if args.profile
        else (
            Path(os.environ.get("LOCALAPPDATA", Path.home()))
            / "CoinGeckoArchive"
            / f"{args.browser}-profile"
        ).resolve()
    )
    executable = _browser_executable(args.browser)
    if not executable:
        raise RuntimeError(f"{args.browser} was not found; refusing to launch a different browser.")
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        executable_path=executable,
        headless=False,
        viewport={"width": 1440, "height": 1000},
        accept_downloads=False,
    )
    page = context.pages[0] if context.pages else context.new_page()
    return None, context, page, True


def _open_whitepaper_sites(
    browser: str,
    profile: Path,
    urls: list[str],
    context: BrowserContext | None = None,
) -> None:
    urls = list(dict.fromkeys(urls))
    if not urls:
        return
    if context is not None:
        for url in urls:
            try:
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(1000)
            except Exception as error:
                print(f"백서 탭 열기 실패: {url} ({type(error).__name__})")
        return
    executable = _browser_executable(browser)
    if not executable:
        print(f"검토 필요: {browser} 실행 파일을 찾지 못해 웹사이트 백서를 새 탭으로 열지 못했습니다.")
        return
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        [executable, f"--user-data-dir={profile}", "--new-window", *urls],
        close_fds=True,
        creationflags=flags,
    )


def _coin_details(
    page: Page, coin: Coin
) -> tuple[str, str | None, str, str | None, str, str | None, str, list[str]]:
    _navigate(page, coin.url)
    heading = page.locator("h1").first
    name = heading.inner_text(timeout=3000).strip() if heading.count() else coin.name
    html = page.content()
    website, website_status = extract_coin_website(html, coin.url)
    whitepaper, whitepaper_status = extract_labeled_link(
        html, {"whitepaper", "white paper"}, coin.url
    )
    twitter, twitter_status = extract_twitter_link(html, coin.url)
    explorers = extract_explorer_links(html, coin.url)
    return (
        name or coin.name,
        website,
        website_status,
        whitepaper,
        whitepaper_status,
        twitter,
        twitter_status,
        explorers,
    )


def _prepare_page_for_capture(page: Page, *, min_wait_ms: int = 1200, max_scroll_rounds: int = 12) -> None:
    """Fully load a scroll-responsive page before taking a full-page screenshot."""
    if not hasattr(page, "evaluate") and not hasattr(page, "wait_for_timeout"):
        return
    try:
        if not hasattr(page, "evaluate"):
            if hasattr(page, "wait_for_timeout"):
                page.wait_for_timeout(min_wait_ms)
            return

        previous_height = -1
        stable_rounds = 0
        for _ in range(max_scroll_rounds):
            height = page.evaluate("document.documentElement.scrollHeight")
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            if hasattr(page, "wait_for_timeout"):
                page.wait_for_timeout(max(300, min_wait_ms // 6))
            next_height = page.evaluate("document.documentElement.scrollHeight")
            if next_height == height == previous_height:
                stable_rounds += 1
                if stable_rounds >= 2:
                    break
            else:
                stable_rounds = 0
            previous_height = next_height

        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        if hasattr(page, "wait_for_timeout"):
            page.wait_for_timeout(min_wait_ms)
    except Exception:
        if hasattr(page, "wait_for_timeout"):
            page.wait_for_timeout(min_wait_ms)


def _save_full_page_screenshot(page: Page, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.stem + ".part" + target.suffix)
    try:
        _prepare_page_for_capture(page)
        retry(lambda: page.screenshot(path=str(temporary), full_page=True, timeout=90_000))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _load_lazy_content(page: Page) -> None:
    stable_rounds = 0
    previous_height = -1
    for _ in range(60):
        _check_access_wall(page)
        height = page.evaluate("document.documentElement.scrollHeight")
        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        page.wait_for_timeout(250)
        next_height = page.evaluate("document.documentElement.scrollHeight")
        if next_height == height == previous_height:
            stable_rounds += 1
            if stable_rounds >= 3:
                break
        else:
            stable_rounds = 0
        previous_height = next_height
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)


def _save_page_as_pdf(page: Page, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.stem + ".part" + target.suffix)
    try:
        session = page.context.new_cdp_session(page)
        try:
            result = session.send(
                "Page.printToPDF",
                {
                    "printBackground": True,
                    "preferCSSPageSize": True,
                    "displayHeaderFooter": False,
                    "transferMode": "ReturnAsBase64",
                },
            )
        finally:
            session.detach()
        data = base64.b64decode(result["data"], validate=True)
        if not is_pdf_content(data[:8]):
            raise ValueError("Browser print output is not a PDF.")
        if len(data) < 1024 or b"%%EOF" not in data[-2048:]:
            raise ValueError("Browser print output is empty or incomplete.")
        if not re.search(rb"/Type\s*/Page\b", data):
            raise ValueError("Browser print output contains no PDF pages.")
        if len(data) > MAX_PDF_BYTES:
            raise ValueError(f"PDF exceeds the {MAX_PDF_BYTES // (1024 * 1024)} MB safety limit")
        temporary.write_bytes(data)
        os.replace(temporary, target)
        _compress_pdf_if_needed(target)
    finally:
        temporary.unlink(missing_ok=True)


def _capture_coin(
    page: Page,
    coin: Coin,
    screenshot_dir: Path,
    pdf_dir: Path,
    output_root: Path,
) -> tuple[dict, str | None]:
    details: dict = {
        "name": coin.name,
        "symbol": coin.symbol,
        "slug": coin.slug,
        "display_name": coin.label(),
        "duplicate_index": coin.duplicate_index,
        "coin_url": coin.url,
        "portfolio": coin.portfolio,
        "page": coin.page,
        "listed_age": coin.added,
        "status": "in_progress",
        "review": [],
    }
    pending_site: str | None = None
    try:
        (
            name,
            website,
            website_status,
            whitepaper,
            whitepaper_status,
            twitter,
            twitter_status,
            explorers,
        ) = _coin_details(page, coin)
    except AccessWallError:
        raise
    except Exception as error:
        details.update(status="needs_review", review=[f"coin_detail_failed:{type(error).__name__}"])
        return details, pending_site
    details.update(
        page_name=" ".join(name.split()) if name else None,
        website_url=website,
        website_status=website_status,
        whitepaper_url=whitepaper,
        whitepaper_status=whitepaper_status,
        twitter_url=twitter,
        twitter_status=twitter_status,
        explorer_urls=explorers,
    )
    captured_target = False
    if website_status == "ok" and website:
        try:
            response = _navigate(page, website)
            if response and response.status >= 400:
                details["review"].append(f"website_http_{response.status}")
            else:
                screenshot = output_path(
                    screenshot_dir,
                    coin.name,
                    f"{coin.url}|{website}",
                    "-website.png",
                    coin.symbol,
                    duplicate_index=getattr(coin, "duplicate_index", 1),
                )
                _save_full_page_screenshot(page, screenshot)
                details["screenshot"] = str(screenshot.relative_to(output_root))
                details["capture_source"] = "website"
                captured_target = True
        except AccessWallError:
            raise
        except Exception as error:
            details["review"].append(f"website_capture_failed:{type(error).__name__}")
    else:
        details["review"].append(f"website_{website_status}")

    if not captured_target and twitter_status == "ok" and twitter:
        try:
            response = _navigate(page, twitter)
            if (response and response.status in {404, 410}) or _twitter_account_unavailable(page):
                details["twitter_status"] = "account_unavailable"
                details["review"].append("twitter_account_unavailable")
            elif response and response.status >= 400:
                details["twitter_status"] = f"http_{response.status}"
                details["review"].append(f"twitter_http_{response.status}")
            else:
                screenshot = output_path(
                    screenshot_dir,
                    coin.name,
                    f"{coin.url}|{twitter}",
                    "-twitter.png",
                    coin.symbol,
                    duplicate_index=getattr(coin, "duplicate_index", 1),
                )
                _save_full_page_screenshot(page, screenshot)
                details["twitter_screenshot"] = str(screenshot.relative_to(output_root))
                details["capture_source"] = "twitter"
                captured_target = True
        except AccessWallError:
            raise
        except Exception as error:
            details["review"].append(f"twitter_capture_failed:{type(error).__name__}")
    elif not captured_target and twitter_status != "ok":
        details["review"].append(f"twitter_{twitter_status}")

    if not captured_target:
        for explorer in explorers:
            try:
                response = _navigate(page, explorer)
                if response and response.status >= 400:
                    details["review"].append(f"explorer_http_{response.status}")
                else:
                    screenshot = output_path(
                        screenshot_dir,
                        coin.name,
                        f"{coin.url}|{explorer}",
                        "-explorer.png",
                        coin.symbol,
                        duplicate_index=getattr(coin, "duplicate_index", 1),
                    )
                    _save_full_page_screenshot(page, screenshot)
                    details.setdefault("explorer_screenshots", []).append(
                        str(screenshot.relative_to(output_root))
                    )
                    details["capture_source"] = "explorer"
                    captured_target = True
            except AccessWallError:
                raise
            except Exception as error:
                details["review"].append(f"explorer_capture_failed:{type(error).__name__}")

    if not captured_target:
        details["review"].append("no_website_twitter_or_explorer_captured")

    if whitepaper_status == "ok" and whitepaper:
        try:
            target = output_path(
                pdf_dir,
                coin.name,
                coin.url,
                ".pdf",
                coin.symbol,
                duplicate_index=getattr(coin, "duplicate_index", 1),
            )
            ok, result = retry(lambda: _download_pdf(whitepaper, target))
            details["whitepaper_result"] = result
            if ok:
                details["pdf"] = str(target.relative_to(output_root))
            elif result.startswith("not_pdf:"):
                pending_site = whitepaper
                try:
                    response = _navigate(page, whitepaper)
                    if response and response.status >= 400:
                        raise ValueError(f"whitepaper_http_{response.status}")
                    _load_lazy_content(page)
                    rendered_pdf = output_path(
                        pdf_dir,
                        coin.name,
                        coin.url,
                        "-whitepaper.pdf",
                        coin.symbol,
                        duplicate_index=getattr(coin, "duplicate_index", 1),
                    )
                    retry(lambda: _save_page_as_pdf(page, rendered_pdf))
                    pending_site = None
                    details["pdf"] = str(rendered_pdf.relative_to(output_root))
                    details["whitepaper_result"] = "rendered_pdf"
                except AccessWallError:
                    raise
                except Exception as error:
                    details["whitepaper_result"] = "render_failed"
                    details["review"].append(f"whitepaper_pdf_failed:{type(error).__name__}")
                    print(f"백서 PDF 생성 실패: {coin.name} ({type(error).__name__})")
            else:
                details["review"].append(f"whitepaper_{result}")
        except AccessWallError as error:
            details["whitepaper_result"] = "access_blocked"
            details["review"].append(f"whitepaper_pdf_failed:{type(error).__name__}")
            print(f"백서 PDF 접근 실패: {coin.name} ({error})")
        except Exception as error:
            details["review"].append(f"whitepaper_download_failed:{type(error).__name__}")
            print(f"백서 다운로드 실패: {coin.name} ({type(error).__name__})")
    else:
        details["review"].append(f"whitepaper_{whitepaper_status}")

    details["status"] = "completed" if not details["review"] else "needs_review"
    return details, pending_site


def _open_setup_profile(executable: str, profile: Path) -> None:
    profile.mkdir(parents=True, exist_ok=True)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        [executable, f"--user-data-dir={profile}", "--new-window", LIST_URL],
        close_fds=True,
        creationflags=flags,
    )
    print(
        f"전용 {profile.parent.name} 프로필 브라우저를 열었습니다. 이 창에서 CoinGecko에 직접 로그인한 뒤 "
        "미리보기를 실행하기 전에 이 전용 창만 닫으세요. 암호는 프로그램이 다루지 않습니다."
    )


def _load_selection(path: str | None) -> set[str] | None:
    if not path:
        return None
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("urls") or data.get("coins") or []
    selected: set[str] = set()
    for item in data:
        value = item.get("coin_url") or item.get("url") if isinstance(item, dict) else item
        if value:
            selected.add(str(value).rstrip("/").lower())
    return selected


def _write_result_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parent
    default_profile = (
        Path(os.environ.get("LOCALAPPDATA", Path.home()))
        / "CoinGeckoArchive"
        / f"{args.browser}-profile"
    )
    profile = Path(args.profile).expanduser().resolve() if args.profile else default_profile.resolve()
    output_root = Path(args.output_dir).expanduser().resolve() if args.output_dir else root
    manifest_path = output_root / ".state" / "manifest.json"
    screenshot_dir = output_root / "archive" / "screenshots"
    pdf_dir = output_root / "archive" / "whitepapers"
    report_path = output_root / "capture-report.txt"
    preview_path = output_root / "recent-coins-preview.csv"

    if args.setup_profile:
        executable = _browser_executable(args.browser)
        if not executable:
            raise RuntimeError(f"{args.browser} was not found; refusing to launch a different browser.")
        _open_setup_profile(executable, profile)
        return 0

    manifest = _load_manifest(manifest_path)
    whitepaper_sites: list[str] = []
    capture_results: list[dict] = []
    favorite_results: list[dict] = []
    all_coins: list[Coin] = []
    selected_urls: set[str] | None = _load_selection(args.selection_json)
    missing_selection: list[str] = []
    endpoint = _browser_session_endpoint(args)
    attached = bool(endpoint)
    result: dict = {
        "version": 1,
        "mode": args.mode,
        "started_at": datetime.now().astimezone().isoformat(),
        "finished_at": None,
        "browser": {
            "mode": "attach" if attached else "profile",
            "browser": args.browser,
            "endpoint": endpoint,
            "profile": None if attached else str(profile),
        },
        "pages": args.pages,
        "since_days": args.since_days,
        "coins": [],
        "selected_urls": sorted(selected_urls) if selected_urls is not None else None,
        "captures": [],
        "favorites": [],
        "errors": [],
    }

    with sync_playwright() as playwright:
        try:
            browser, context, page, owns_context = _open_browser_session(playwright, args)
        except PlaywrightError as error:
            if "ProcessSingleton" in str(error):
                raise RuntimeError(
                    f"The dedicated {args.browser} profile is already open: {profile}. "
                    f"Close only that automation {args.browser} window and retry; other browser windows are unaffected."
                ) from error
            raise
        try:
            if not attached:
                print(
                    "실사용 Chrome 디버깅 주소를 찾지 못해 전용 브라우저 프로필을 사용합니다. "
                    "이 프로필은 평소 Chrome 로그인과 공유되지 않습니다."
                )
            for number in range(1, args.pages + 1):
                try:
                    coins = retry(lambda: _listing_page(page, number))
                except AccessWallError:
                    raise
                except Exception as error:
                    print(
                        f"페이지 {number}: 읽기 실패 "
                        f"({type(error).__name__}: {error}); 다음 실행에서 재시도합니다."
                    )
                    result["errors"].append(
                        {
                            "page": number,
                            "error": type(error).__name__,
                            "message": str(error),
                        }
                    )
                    manifest["pages"] = [
                        entry for entry in manifest["pages"] if entry.get("page") != number
                    ]
                    manifest["pages"].append({"page": number, "status": "failed"})
                    _save_manifest(manifest_path, manifest)
                    continue
                if coins and all(coin.portfolio == "unknown" for coin in coins):
                    print(
                        "로그인되지 않았거나 포트폴리오 별표 상태를 확인할 수 없습니다. "
                        "실사용 Chrome 연결 또는 전용 프로필 로그인을 확인하세요."
                    )
                all_coins.extend(coins)
                print(f"페이지 {number}: {len(coins)}개 항목")
                manifest["pages"] = [
                    entry for entry in manifest["pages"] if entry.get("page") != number
                ]
                manifest["pages"].append(
                    {"page": number, "status": "read" if coins else "empty", "count": len(coins)}
                )
                if args.mode == "capture":
                    for coin in coins:
                        manifest["coins"].setdefault(
                            coin.url,
                            {
                                "name": coin.name,
                                "symbol": coin.symbol,
                                "display_name": coin.label(),
                                "coin_url": coin.url,
                                "portfolio": coin.portfolio,
                                "page": coin.page,
                                "status": "listed",
                            },
                        )
                    _save_manifest(manifest_path, manifest)

            all_coins = deduplicate_coins(all_coins)
            before_filter = len(all_coins)
            if args.since_days:
                all_coins = [
                    coin for coin in all_coins if age_within_days(coin.added, args.since_days)
                ]
                age_summary = f"최근 {args.since_days}일 날짜 판독 가능 항목"
            else:
                age_summary = "요청 페이지 전체 항목"
            all_coins = sorted(all_coins, key=_recent_sort_key)
            all_coins = assign_duplicate_indices(all_coins)
            print(f"{age_summary}: {len(all_coins)}개 / {before_filter}개")
            print(f"중복 제거 후 총 {len(all_coins)}개 코인")
            for coin in all_coins:
                print(
                    f"- [{coin.portfolio}] {coin.label()} | {coin.added or '날짜 미확인'} "
                    f"| {coin.url} | page {coin.page}"
                )

            _save_preview(preview_path, all_coins)
            print(f"당일 코인 목록 CSV 저장: {preview_path}")
            result["coins"] = [coin.as_dict() for coin in all_coins]

            selected = all_coins
            if selected_urls is not None:
                selected = [
                    coin
                    for coin in all_coins
                    if coin.url.rstrip("/").lower() in selected_urls
                ]
                found = {coin.url.rstrip("/").lower() for coin in selected}
                missing_selection = sorted(selected_urls - found)
                if missing_selection:
                    print(f"선택한 URL 중 현재 목록에 없는 항목: {len(missing_selection)}개")
                result["missing_selection"] = missing_selection

            if args.mode == "dry-run":
                print("미리보기 완료: 파일 다운로드나 캡처는 수행하지 않았습니다.")
            elif args.mode == "favorites":
                if not _is_authenticated(page):
                    raise RuntimeError(
                        "즐겨찾기를 변경하려면 CoinGecko에 로그인된 실사용 Chrome 세션이 필요합니다."
                    )
                favorite_results = add_coins_to_favorites(page, selected)
                result["favorites"] = favorite_results
                for item in favorite_results:
                    existing = manifest["coins"].get(item["coin_url"], {})
                    existing.update(
                        name=item.get("display_name") or item["coin_url"],
                        display_name=item.get("display_name"),
                        symbol=item.get("symbol"),
                        coin_url=item["coin_url"],
                        favorite_status=item["status"],
                    )
                    if item["status"] in {"added", "already_active"}:
                        existing["portfolio"] = "active"
                    manifest["coins"][item["coin_url"]] = existing
                _save_manifest(manifest_path, manifest)
                added = sum(1 for item in favorite_results if item["status"] == "added")
                print(f"즐겨찾기 추가: {added}개 / 처리 {len(favorite_results)}개")
            else:
                selected, auto_favorite_results = _auto_add_inactive_coins(page, selected)
                if auto_favorite_results:
                    result["favorites"].extend(auto_favorite_results)
                    for item in auto_favorite_results:
                        existing = manifest["coins"].get(item["coin_url"], {})
                        existing.update(
                            name=item.get("display_name") or item["coin_url"],
                            display_name=item.get("display_name"),
                            symbol=item.get("symbol"),
                            coin_url=item["coin_url"],
                            favorite_status=item["status"],
                        )
                        if item["status"] in {"added", "already_active"}:
                            existing["portfolio"] = "active"
                        manifest["coins"][item["coin_url"]] = existing
                    _save_manifest(manifest_path, manifest)
                    added = sum(1 for item in auto_favorite_results if item["status"] == "added")
                    print(f"원클릭 수집 전 자동 즐겨찾기: {added}개 추가 / 처리 {len(auto_favorite_results)}개")
                auto_added_urls = {
                    item["coin_url"].rstrip("/").lower()
                    for item in auto_favorite_results
                    if isinstance(item, dict)
                    and item.get("status") == "added"
                    and item.get("coin_url")
                }
                for coin in selected:
                    if is_tokenized_stock_name(coin.name, coin.symbol):
                        print(f"즐겨찾기 전용 처리(Tokenized Stock): {coin.label()}")
                        continue
                    auto_added = coin.url.rstrip("/").lower() in auto_added_urls
                    if not _capture_allowed(coin, args.include_unstarred, auto_added=auto_added):
                        print(f"건너뜀(새 즐겨찾기 추가 확인 안 됨): {coin.label()}")
                        if coin.portfolio == "unknown":
                            existing = manifest["coins"].get(coin.url, {})
                            existing.update(
                                name=coin.name,
                                symbol=coin.symbol,
                                display_name=coin.label(),
                                coin_url=coin.url,
                                portfolio="unknown",
                                status="needs_review",
                            )
                            review = existing.setdefault("review", [])
                            if "portfolio_state_unknown_fail_closed" not in review:
                                review.append("portfolio_state_unknown_fail_closed")
                            manifest["coins"][coin.url] = existing
                        continue
                    existing = manifest["coins"].get(coin.url, {})
                    if is_complete(existing) and not args.force:
                        print(f"건너뜀(완료됨): {coin.label()}")
                        continue
                    details, pending_site = _capture_coin(
                        page, coin, screenshot_dir, pdf_dir, output_root
                    )
                    manifest["coins"][coin.url] = details
                    capture_results.append(details)
                    result["captures"] = capture_results
                    _save_capture_report(report_path, capture_results)
                    if pending_site:
                        whitepaper_sites.append(pending_site)
                    _save_manifest(manifest_path, manifest)
                    print(f"{details['status']}: {coin.label()}")
                    page.wait_for_timeout(1500)
        finally:
            if owns_context:
                context.close()
            else:
                try:
                    page.close()
                except Exception:
                    pass

    if whitepaper_sites:
        _open_whitepaper_sites(args.browser, profile, whitepaper_sites, context=context)

    result["finished_at"] = datetime.now().astimezone().isoformat()
    result["paths"] = {
        "preview": str(preview_path.relative_to(output_root)),
        "report": str(report_path.relative_to(output_root)),
        "manifest": str(manifest_path.relative_to(output_root)),
    }
    if args.result_json:
        _write_result_json(Path(args.result_json).expanduser().resolve(), result)
    print(f"결과 목록: {manifest_path}")
    print(f"수집 리포트: {report_path}")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Fail-closed CoinGecko portfolio coin archiver")
    parser.add_argument("--mode", choices=("dry-run", "capture", "favorites"), default="dry-run")
    parser.add_argument("--pages", type=int, default=10)
    parser.add_argument(
        "--since-days",
        type=int,
        default=0,
        help="Filter rows by listing age in days; 0 includes every row from the requested pages.",
    )
    parser.add_argument("--browser", choices=("chrome", "edge"), default="chrome")
    parser.add_argument("--profile")
    parser.add_argument("--output-dir")
    parser.add_argument("--setup-profile", action="store_true")
    parser.add_argument(
        "--attach",
        action="store_true",
        help="Require attachment to the running browser through chrome://inspect remote debugging.",
    )
    parser.add_argument("--cdp-url", help="Explicit CDP WebSocket URL for --attach.")
    parser.add_argument(
        "--chrome-user-data",
        help="Browser User Data directory used to find DevToolsActivePort.",
    )
    parser.add_argument(
        "--selection-json",
        help="Only process coin URLs listed in this JSON file (string list or {coin_url}).",
    )
    parser.add_argument(
        "--result-json",
        help="Write a machine-readable run result (coins, captures, favorites).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-capture coins whose manifest status is already completed.",
    )
    parser.add_argument(
        "--include-unstarred",
        action="store_true",
        help="Capture all recent coins regardless of login or portfolio-star state.",
    )
    args = parser.parse_args()
    if not 1 <= args.pages <= 10:
        parser.error("--pages must be between 1 and 10")
    if not 0 <= args.since_days <= 30:
        parser.error("--since-days must be between 0 and 30")
    if args.selection_json and not Path(args.selection_json).expanduser().exists():
        parser.error("--selection-json file does not exist")
    try:
        return run(args)
    except AccessWallError as error:
        print(f"중단: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"실행 실패: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
