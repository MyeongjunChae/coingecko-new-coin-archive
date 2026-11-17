from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError

from coingecko_archive import (
    AccessWallError,
    _download_pdf,
    _download_pdf_attachments,
    _load_manifest,
    _navigate,
    _open_browser_session,
    _save_full_page_screenshot,
    _save_manifest,
    _save_page_as_pdf,
    _write_result_json,
    is_public_http_url,
    output_path,
    sync_playwright,
)


def _safe_entries(path: Path) -> list[dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("북마크 목록은 배열이어야 합니다.")
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        if not is_public_http_url(url):
            continue
        parts = urlsplit(url)
        key = url.rstrip("/").casefold()
        if key in seen:
            continue
        seen.add(key)
        title = " ".join(str(item.get("title", "")).split()) or parts.hostname
        entries.append({"url": url, "title": title[:180]})
    return entries


def _bookmark_label(title: str, url: str) -> str:
    parts = urlsplit(url)
    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", title).strip(" .")
    return (safe_title or parts.hostname or "document")[:80]


def _capture_entry(page, entry: dict[str, str], output_dir: Path) -> dict:
    url = entry["url"]
    title = entry["title"]
    result = {
        "url": url,
        "title": title,
        "status": "in_progress",
        "attachments": [],
        "review": [],
    }
    parts = urlsplit(url)
    try:
        response = _navigate(page, url)
        status = response.status if response else None
        content_type = (
            response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if response
            else ""
        )
        if status is not None and status >= 400:
            result.update(status="skipped_inaccessible", reason=f"http_{status}")
            return result

        label = _bookmark_label(title, url)
        pdf_dir = output_dir / "archive" / "bookmarks" / "pdf"
        screenshot_dir = output_dir / "archive" / "bookmarks" / "screenshots"
        is_pdf = content_type == "application/pdf" or parts.path.casefold().endswith(".pdf")
        if is_pdf:
            target = output_path(pdf_dir, label, url, "-source.pdf")
            ok, reason = _download_pdf(url, target)
            if not ok:
                result.update(status="skipped_inaccessible", reason=reason)
                return result
            result["pdf"] = str(target.relative_to(output_dir))
            result["status"] = "captured"
            return result

        screenshot = output_path(screenshot_dir, label, url, "-page.png")
        _save_full_page_screenshot(page, screenshot)
        result["screenshot"] = str(screenshot.relative_to(output_dir))
        html = page.content()
        rendered_pdf = output_path(pdf_dir, label, url, "-page.pdf")
        _save_page_as_pdf(page, rendered_pdf)
        result["pdf"] = str(rendered_pdf.relative_to(output_dir))

        attachments, issues = _download_pdf_attachments(
            html,
            url,
            label,
            "",
            url,
            pdf_dir,
            output_dir,
        )
        result["attachments"] = attachments
        result["review"] = issues
        result["status"] = "captured" if not issues else "captured_with_issues"
        return result
    except AccessWallError as error:
        result.update(status="skipped_inaccessible", reason=str(error))
        return result
    except Exception as error:
        result.update(
            status="failed",
            error=f"{type(error).__name__}: {error}",
        )
        return result


def run(bookmarks_json: Path, result_json: Path, output_dir: Path) -> int:
    entries = _safe_entries(bookmarks_json)
    if not entries:
        raise ValueError("처리할 수 있는 HTTP/HTTPS 북마크가 없습니다.")

    manifest_path = output_dir / ".state" / "manifest.json"
    manifest = _load_manifest(manifest_path)
    manifest.setdefault("document_bookmarks", {})
    payload = {
        "version": 1,
        "mode": "bookmark-capture",
        "started_at": datetime.now().astimezone().isoformat(),
        "finished_at": None,
        "bookmarks": [],
    }
    args = SimpleNamespace(
        browser="chrome",
        attach=True,
        cdp_url=None,
        chrome_user_data=None,
        profile=None,
    )
    with sync_playwright() as playwright:
        _browser, context, page, owns_context = _open_browser_session(playwright, args)
        try:
            for entry in entries:
                if page.is_closed():
                    page = context.new_page()
                record = _capture_entry(page, entry, output_dir)
                payload["bookmarks"].append(record)
                manifest["document_bookmarks"][entry["url"]] = record
                _save_manifest(manifest_path, manifest)
                _write_result_json(result_json, payload)
                print(f"{record['status']}: {entry['title']} - {entry['url']}")
                try:
                    page.wait_for_timeout(800)
                except PlaywrightError:
                    page = context.new_page()
        finally:
            if owns_context:
                context.close()
            else:
                try:
                    page.close()
                except PlaywrightError:
                    pass

    payload["finished_at"] = datetime.now().astimezone().isoformat()
    _write_result_json(result_json, payload)
    summary = {}
    for item in payload["bookmarks"]:
        summary[item["status"]] = summary.get(item["status"], 0) + 1
    payload["summary"] = summary
    _write_result_json(result_json, payload)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture document pages from Chrome bookmarks.")
    parser.add_argument("--bookmarks-json", required=True, type=Path)
    parser.add_argument("--result-json", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        return run(args.bookmarks_json, args.result_json, args.output_dir)
    except Exception as error:
        print(f"북마크 문서 수집 실패: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
