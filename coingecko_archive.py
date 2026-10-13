from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
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
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

LIST_URL = "https://www.coingecko.com/en/new-cryptocurrencies"
MAX_PDF_BYTES = 100 * 1024 * 1024
RETRIES = 3
T = TypeVar("T")


@dataclass(frozen=True)
class Coin:
    name: str
    url: str
    portfolio: str
    page: int
    added: str | None = None


class AccessWallError(RuntimeError):
    pass


def page_url(page_number: int, base_url: str = LIST_URL) -> str:
    parts = urlsplit(base_url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "page"]
    query.append(("page", str(page_number)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _control_identity(control: Tag) -> bool:
    labels = " ".join(
        str(control.get(attribute, ""))
        for attribute in ("aria-label", "title", "data-testid", "class")
    ).lower()
    return any(word in labels for word in ("star", "favorite", "favourite", "watchlist", "portfolio"))


def _control_state(control: Tag) -> str:
    values: list[str] = []
    for attribute in ("aria-pressed", "aria-checked", "data-state", "data-active", "data-favorited"):
        value = control.get(attribute)
        if value is not None:
            values.append(str(value).strip().lower())

    label = " ".join(
        str(control.get(attribute, ""))
        for attribute in ("aria-label", "title", "data-testid")
    ).strip().lower()
    if re.search(r"\b(remove|unfollow|unfavorite|un-favorite|unstar|un-star)\b", label):
        values.append("active")
    elif re.search(r"\b(add|follow)\b", label):
        values.append("inactive")

    active_values = {"true", "checked", "selected", "on", "active", "1"}
    inactive_values = {"false", "unchecked", "off", "inactive", "0"}
    states = {
        "active" if value in active_values else "inactive"
        for value in values
        if value in active_values or value in inactive_values
    }
    return states.pop() if len(states) == 1 else "unknown"


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
        name = " ".join(anchor.get_text(" ", strip=True).split())
        if not name:
            name = str(anchor.get("aria-label", "")).strip()
        if not name:
            continue
        seen.add(canonical)
        coins.append(
            Coin(
                name,
                canonical,
                _portfolio_state(row),
                page_number,
                _row_age(row),
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
        unique[key] = Coin(coin.name, coin.url, "unknown", coin.page, coin.added)
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


def output_path(directory: Path, name: str, url: str, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{sanitize_filename(name)}-{coin_key(url)}{suffix}"


def is_pdf_content(prefix: bytes) -> bool:
    return prefix.startswith(b"%PDF-")


def is_complete(record: dict) -> bool:
    return record.get("status") == "completed"


def _capture_allowed(coin: Coin, include_unstarred: bool) -> bool:
    return include_unstarred or coin.portfolio == "active"


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


def _save_preview(path: Path, coins: Iterable[Coin]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=("name", "coin_url", "listed_age", "portfolio_state", "page"),
            )
            writer.writeheader()
            for coin in coins:
                writer.writerow(
                    {
                        "name": coin.name,
                        "coin_url": coin.url,
                        "listed_age": coin.added or "",
                        "portfolio_state": coin.portfolio,
                        "page": coin.page,
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
    return page.get_by_text("Login", exact=True).count() == 0 and page.get_by_text(
        "Sign up", exact=True
    ).count() == 0


def _navigate(page: Page, url: str) -> None:
    retry(lambda: page.goto(url, wait_until="domcontentloaded", timeout=45_000))
    _check_access_wall(page)
    page.wait_for_timeout(1800)


def _listing_page(page: Page, number: int) -> list[Coin]:
    url = page_url(number)
    _navigate(page, url)
    try:
        page.wait_for_selector("a[href*='/coins/']", timeout=15_000)
    except PlaywrightTimeoutError:
        _check_access_wall(page)
    coins = parse_listing_html(page.content(), number, url)
    if not _is_authenticated(page):
        return [Coin(coin.name, coin.url, "unknown", coin.page, coin.added) for coin in coins]
    return coins


def _download_pdf(url: str, target: Path) -> tuple[bool, str]:
    temporary = target.with_suffix(target.suffix + ".part")
    try:
        with requests.get(url, stream=True, timeout=(15, 45), allow_redirects=True) as response:
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


def _open_whitepaper_sites(browser: str, profile: Path, urls: list[str]) -> None:
    urls = list(dict.fromkeys(urls))
    if not urls:
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


def _coin_details(page: Page, coin: Coin) -> tuple[str, str | None, str, str | None, str]:
    _navigate(page, coin.url)
    heading = page.locator("h1").first
    name = heading.inner_text(timeout=3000).strip() if heading.count() else coin.name
    html = page.content()
    website, website_status = extract_labeled_link(
        html, {"website", "official website"}, coin.url
    )
    whitepaper, whitepaper_status = extract_labeled_link(
        html, {"whitepaper", "white paper"}, coin.url
    )
    return name or coin.name, website, website_status, whitepaper, whitepaper_status


def _save_full_page_screenshot(page: Page, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.stem + ".part" + target.suffix)
    try:
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
        if len(data) > MAX_PDF_BYTES:
            raise ValueError(f"PDF exceeds the {MAX_PDF_BYTES // (1024 * 1024)} MB safety limit")
        temporary.write_bytes(data)
        os.replace(temporary, target)
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
        "coin_url": coin.url,
        "portfolio": coin.portfolio,
        "page": coin.page,
        "listed_age": coin.added,
        "status": "in_progress",
        "review": [],
    }
    pending_site: str | None = None
    try:
        name, website, website_status, whitepaper, whitepaper_status = _coin_details(page, coin)
    except AccessWallError:
        raise
    except Exception as error:
        details.update(status="needs_review", review=[f"coin_detail_failed:{type(error).__name__}"])
        return details, pending_site
    details.update(
        name=name,
        website_url=website,
        website_status=website_status,
        whitepaper_url=whitepaper,
        whitepaper_status=whitepaper_status,
    )
    try:
        coin_screenshot = output_path(screenshot_dir, coin.name, coin.url, "-coingecko.png")
        _save_full_page_screenshot(page, coin_screenshot)
        details["coin_screenshot"] = str(coin_screenshot.relative_to(output_root))
    except AccessWallError:
        raise
    except Exception as error:
        details["review"].append(f"coin_screenshot_failed:{type(error).__name__}")

    if website_status != "ok" or not website:
        details["review"].append(f"website_{website_status}")
    else:
        try:
            _navigate(page, website)
            screenshot = output_path(screenshot_dir, coin.name, coin.url, ".png")
            _save_full_page_screenshot(page, screenshot)
            details["screenshot"] = str(screenshot.relative_to(output_root))
        except AccessWallError:
            raise
        except Exception as error:
            details["review"].append(f"website_capture_failed:{type(error).__name__}")

    if whitepaper_status == "ok" and whitepaper:
        try:
            target = output_path(pdf_dir, coin.name, coin.url, ".pdf")
            ok, result = retry(lambda: _download_pdf(whitepaper, target))
            details["whitepaper_result"] = result
            if ok:
                details["pdf"] = str(target.relative_to(output_root))
            elif result.startswith("not_pdf:"):
                pending_site = whitepaper
                details["whitepaper_result"] = "website"
                _navigate(page, whitepaper)
                _load_lazy_content(page)
                screenshot = output_path(pdf_dir, coin.name, coin.url, "-whitepaper.png")
                _save_full_page_screenshot(page, screenshot)
                details["whitepaper_screenshot"] = str(screenshot.relative_to(output_root))
                rendered_pdf = output_path(pdf_dir, coin.name, coin.url, "-whitepaper.pdf")
                _save_page_as_pdf(page, rendered_pdf)
                details["pdf"] = str(rendered_pdf.relative_to(output_root))
                details["whitepaper_result"] = "rendered_pdf"
            else:
                details["review"].append(f"whitepaper_{result}")
        except AccessWallError:
            raise
        except Exception as error:
            details["review"].append(f"whitepaper_download_failed:{type(error).__name__}")
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


def run(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parent
    default_profile = (
        Path(os.environ.get("LOCALAPPDATA", Path.home()))
        / "CoinGeckoArchive"
        / f"{args.browser}-profile"
    )
    profile = Path(args.profile).expanduser().resolve() if args.profile else default_profile.resolve()
    executable = _browser_executable(args.browser)
    if not executable:
        raise RuntimeError(f"{args.browser} was not found; refusing to launch a different browser.")
    if args.setup_profile:
        _open_setup_profile(executable, profile)
        return 0
    output_root = Path(args.output_dir).expanduser().resolve() if args.output_dir else root
    manifest_path = output_root / ".state" / "manifest.json"
    screenshot_dir = output_root / "archive" / "screenshots"
    pdf_dir = output_root / "archive" / "whitepapers"
    manifest = _load_manifest(manifest_path)
    whitepaper_sites: list[str] = []
    all_coins: list[Coin] = []

    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                executable_path=executable,
                headless=False,
                viewport={"width": 1440, "height": 1000},
                accept_downloads=False,
            )
        except PlaywrightError as error:
            if "ProcessSingleton" in str(error):
                raise RuntimeError(
                    f"The dedicated {args.browser} profile is already open: {profile}. "
                    f"Close only that automation {args.browser} window and retry; other browser windows are unaffected."
                ) from error
            raise
        try:
            page = context.pages[0] if context.pages else context.new_page()
            for number in range(1, args.pages + 1):
                try:
                    coins = retry(lambda: _listing_page(page, number))
                except AccessWallError:
                    raise
                except Exception as error:
                    print(f"페이지 {number}: 읽기 실패 ({type(error).__name__}); 다음 실행에서 재시도합니다.")
                    manifest["pages"] = [
                        entry for entry in manifest["pages"] if entry.get("page") != number
                    ]
                    manifest["pages"].append({"page": number, "status": "failed"})
                    _save_manifest(manifest_path, manifest)
                    continue
                if coins and all(coin.portfolio == "unknown" for coin in coins):
                    print(
                        "로그인되지 않았거나 포트폴리오 별표 상태를 확인할 수 없습니다. "
                        "이번 실행은 미리보기만 가능하며 캡처는 진행하지 않습니다."
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
                                "coin_url": coin.url,
                                "portfolio": coin.portfolio,
                                "page": coin.page,
                                "status": "listed",
                            },
                        )
                    _save_manifest(manifest_path, manifest)

            all_coins = deduplicate_coins(all_coins)
            before_filter = len(all_coins)
            all_coins = sorted(
                (coin for coin in all_coins if age_within_days(coin.added, args.since_days)),
                key=_recent_sort_key,
            )
            print(
                f"최근 {args.since_days}일 날짜 판독 가능 항목: "
                f"{len(all_coins)}개 / {before_filter}개"
            )
            print(f"중복 제거 후 총 {len(all_coins)}개 코인")
            for coin in all_coins:
                print(
                    f"- [{coin.portfolio}] {coin.name} | {coin.added or '날짜 미확인'} "
                    f"| {coin.url} | page {coin.page}"
                )

            preview_path = output_root / "recent-coins-preview.csv"
            _save_preview(preview_path, all_coins)
            print(f"당일 코인 목록 CSV 저장: {preview_path}")

            if args.mode == "dry-run":
                print("미리보기 완료: 파일 다운로드나 캡처는 수행하지 않았습니다.")
                return 0

            for coin in all_coins:
                if not _capture_allowed(coin, args.include_unstarred):
                    if coin.portfolio == "unknown":
                        existing = manifest["coins"].get(coin.url, {})
                        existing.update(
                            name=coin.name,
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
                if is_complete(existing):
                    print(f"건너뜀(완료됨): {coin.name}")
                    continue
                details, pending_site = _capture_coin(
                    page, coin, screenshot_dir, pdf_dir, output_root
                )
                manifest["coins"][coin.url] = details
                if pending_site:
                    whitepaper_sites.append(pending_site)
                _save_manifest(manifest_path, manifest)
                print(f"{details['status']}: {coin.name}")
                page.wait_for_timeout(1500)
        finally:
            context.close()

    _open_whitepaper_sites(args.browser, profile, whitepaper_sites)
    print(f"결과 목록: {manifest_path}")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Fail-closed CoinGecko portfolio coin archiver")
    parser.add_argument("--mode", choices=("dry-run", "capture"), default="dry-run")
    parser.add_argument("--pages", type=int, default=10)
    parser.add_argument("--since-days", type=int, default=1)
    parser.add_argument("--browser", choices=("chrome", "edge"), default="chrome")
    parser.add_argument("--profile")
    parser.add_argument("--output-dir")
    parser.add_argument("--setup-profile", action="store_true")
    parser.add_argument(
        "--include-unstarred",
        action="store_true",
        help="Capture all recent coins regardless of login or portfolio-star state.",
    )
    args = parser.parse_args()
    if not 1 <= args.pages <= 10:
        parser.error("--pages must be between 1 and 10")
    if not 1 <= args.since_days <= 30:
        parser.error("--since-days must be between 1 and 30")
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
