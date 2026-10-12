from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
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


def _listing_container(anchor: Tag) -> Tag:
    for parent in anchor.parents:
        if not isinstance(parent, Tag):
            continue
        if parent.name == "tr" or parent.get("role") == "row":
            return parent
        if parent.name in {"body", "html"}:
            break
    parent = anchor.parent
    return parent if isinstance(parent, Tag) else anchor


def _portfolio_state(container: Tag) -> str:
    candidates: list[str] = []
    for control in container.select("button, [role='button'], [aria-pressed], [aria-checked]"):
        if _control_identity(control):
            candidates.append(_control_state(control))
    states = set(candidates)
    if not candidates or "unknown" in states or len(states) != 1:
        return "unknown"
    return candidates[0]


def parse_listing_html(html: str, page_number: int, base_url: str = LIST_URL) -> list[Coin]:
    soup = BeautifulSoup(html, "html.parser")
    coins: list[Coin] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = urljoin(base_url, str(anchor["href"]))
        parts = urlsplit(href)
        if not re.search(r"/coins/[^/]+/?$", parts.path):
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
        coins.append(Coin(name, canonical, _portfolio_state(_listing_container(anchor)), page_number))
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
        unique[key] = Coin(coin.name, coin.url, "unknown", coin.page)
    return list(unique.values())


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


def retry(operation: Callable[[], T], attempts: int = RETRIES, delay: float = 1.0) -> T:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return operation()
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
    return parse_listing_html(page.content(), number, url)


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


def _chrome_executable() -> str | None:
    for environment in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(environment)
        if root:
            candidate = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if candidate.exists():
                return str(candidate)
    return None


def _open_whitepaper_sites(profile: Path, urls: list[str]) -> None:
    urls = list(dict.fromkeys(urls))
    if not urls:
        return
    executable = _chrome_executable()
    if not executable:
        print("검토 필요: Chrome 실행 파일을 찾지 못해 웹사이트 백서를 새 탭으로 열지 못했습니다.")
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


def _capture_coin(
    page: Page,
    coin: Coin,
    screenshot_dir: Path,
    pdf_dir: Path,
) -> tuple[dict, str | None]:
    details: dict = {
        "name": coin.name,
        "coin_url": coin.url,
        "portfolio": coin.portfolio,
        "page": coin.page,
        "status": "in_progress",
        "review": [],
    }
    pending_site: str | None = None
    try:
        name, website, website_status, whitepaper, whitepaper_status = retry(
            lambda: _coin_details(page, coin)
        )
        details.update(
            name=name,
            website_url=website,
            website_status=website_status,
            whitepaper_url=whitepaper,
            whitepaper_status=whitepaper_status,
        )
    except Exception as error:
        details.update(status="needs_review", review=[f"coin_detail_failed:{type(error).__name__}"])
        return details, pending_site

    if website_status != "ok" or not website:
        details["review"].append(f"website_{website_status}")
    else:
        try:
            _navigate(page, website)
            screenshot = output_path(screenshot_dir, coin.name, coin.url, ".png")
            temporary = screenshot.with_name(screenshot.stem + ".part.png")
            try:
                retry(lambda: page.screenshot(path=str(temporary), full_page=True, timeout=90_000))
                os.replace(temporary, screenshot)
            finally:
                temporary.unlink(missing_ok=True)
            details["screenshot"] = str(screenshot.relative_to(Path(__file__).resolve().parent))
        except Exception as error:
            details["review"].append(f"website_capture_failed:{type(error).__name__}")

    if whitepaper_status == "ok" and whitepaper:
        try:
            target = output_path(pdf_dir, coin.name, coin.url, ".pdf")
            ok, result = retry(lambda: _download_pdf(whitepaper, target))
            details["whitepaper_result"] = result
            if ok:
                details["pdf"] = str(target.relative_to(Path(__file__).resolve().parent))
            elif result.startswith("not_pdf:"):
                pending_site = whitepaper
                details["whitepaper_result"] = "website"
            else:
                details["review"].append(f"whitepaper_{result}")
        except Exception as error:
            details["review"].append(f"whitepaper_download_failed:{type(error).__name__}")
    else:
        details["review"].append(f"whitepaper_{whitepaper_status}")

    details["status"] = "completed" if not details["review"] else "needs_review"
    return details, pending_site


def _run_setup(context: BrowserContext) -> None:
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(LIST_URL, wait_until="domcontentloaded", timeout=45_000)
    print(
        "열린 전용 Chrome 창에서 CoinGecko에 직접 로그인하고 포트폴리오 별표 상태를 확인한 뒤 "
        "이 터미널로 돌아와 Enter를 누르세요. 암호는 이 도구에 입력하거나 저장하지 않습니다."
    )
    input()


def run(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parent
    profile = Path(args.profile).expanduser().resolve()
    executable = _chrome_executable()
    if not executable:
        raise RuntimeError("Google Chrome was not found; refusing to launch a different browser.")
    manifest_path = root / ".state" / "manifest.json"
    screenshot_dir = root / "archive" / "screenshots"
    pdf_dir = root / "archive" / "whitepapers"
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
                    f"The dedicated Chrome profile is already open: {profile}. "
                    "Close only that automation Chrome window and retry; other Chrome windows are unaffected."
                ) from error
            raise
        try:
            if args.setup_profile:
                _run_setup(context)
                return 0
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
            print(f"중복 제거 후 총 {len(all_coins)}개 코인")
            for coin in all_coins:
                print(f"- [{coin.portfolio}] {coin.name} | {coin.url} | page {coin.page}")

            if args.mode == "dry-run":
                print("미리보기 완료: 파일 다운로드나 캡처는 수행하지 않았습니다.")
                return 0

            for coin in all_coins:
                if coin.portfolio != "active":
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
                details, pending_site = _capture_coin(page, coin, screenshot_dir, pdf_dir)
                manifest["coins"][coin.url] = details
                if pending_site:
                    whitepaper_sites.append(pending_site)
                _save_manifest(manifest_path, manifest)
                print(f"{details['status']}: {coin.name}")
                page.wait_for_timeout(1500)
        finally:
            context.close()

    _open_whitepaper_sites(profile, whitepaper_sites)
    print(f"결과 목록: {manifest_path}")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Fail-closed CoinGecko portfolio coin archiver")
    parser.add_argument("--mode", choices=("dry-run", "capture"), default="dry-run")
    parser.add_argument("--pages", type=int, default=10)
    parser.add_argument(
        "--profile",
        default=str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CoinGeckoArchive" / "chrome-profile"),
    )
    parser.add_argument("--setup-profile", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.pages <= 10:
        parser.error("--pages must be between 1 and 10")
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
