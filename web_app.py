from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
import zipfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from coingecko_archive import (
    _browser_executable,
    browser_user_data_dir,
    chrome_debug_endpoint,
    symbol_in_name,
)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.environ.get("COINGECKO_ARCHIVE_OUTPUT", BASE_DIR.parent)).resolve()
STATE_DIR = OUTPUT_DIR / ".state"
LOG_DIR = STATE_DIR / "web-logs"
TASK_NAME = "CoinGecko-Weekly-Listing"
DEFAULT_PORT = int(os.environ.get("COINGECKO_WEB_PORT", "8765"))

LAST_SCAN = STATE_DIR / "web-last-scan.json"
LAST_RESULT = STATE_DIR / "web-last-result.json"
LAST_FAVORITES = STATE_DIR / "web-last-favorites.json"
SELECTION = STATE_DIR / "web-selection.json"


def configure(output_dir: Path) -> None:
    global OUTPUT_DIR, STATE_DIR, LOG_DIR
    global LAST_SCAN, LAST_RESULT, LAST_FAVORITES, SELECTION
    OUTPUT_DIR = output_dir.expanduser().resolve()
    STATE_DIR = OUTPUT_DIR / ".state"
    LOG_DIR = STATE_DIR / "web-logs"
    LAST_SCAN = STATE_DIR / "web-last-scan.json"
    LAST_RESULT = STATE_DIR / "web-last-result.json"
    LAST_FAVORITES = STATE_DIR / "web-last-favorites.json"
    SELECTION = STATE_DIR / "web-selection.json"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def chrome_status(browser: str = "chrome") -> dict:
    endpoint = chrome_debug_endpoint(browser)
    reachable = False
    if endpoint:
        try:
            parts = urlsplit(endpoint)
            with socket.create_connection((parts.hostname or "127.0.0.1", parts.port), timeout=0.6):
                reachable = True
        except (OSError, ValueError):
            reachable = False
    return {
        "browser": browser,
        "user_data_dir": str(browser_user_data_dir(browser)),
        "remote_debugging_enabled": endpoint is not None,
        "reachable": reachable,
        "endpoint": endpoint,
    }


_schedule_cache: tuple[float, dict] | None = None


def schedule_status(force: bool = False) -> dict:
    global _schedule_cache
    if not force and _schedule_cache and time.monotonic() - _schedule_cache[0] < 20:
        return _schedule_cache[1]
    command = (
        f"(Get-ScheduledTask -TaskName '{TASK_NAME}' "
        "-ErrorAction SilentlyContinue).State"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        state = (result.stdout or "").strip() or "NotFound"
    except (OSError, subprocess.SubprocessError):
        state = "Unknown"
    payload = {
        "task": TASK_NAME,
        "state": state,
        "enabled": state.casefold() not in {"disabled", "notfound", "unknown", ""},
    }
    if state.casefold() != "unknown":
        _schedule_cache = (time.monotonic(), payload)
    return payload


@dataclass
class Job:
    id: str
    kind: str
    command: list[str]
    status: str = "running"
    started_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    returncode: int | None = None
    cancelled: bool = False
    error: str | None = None
    result: dict | list | None = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=500))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "returncode": self.returncode,
            "error": self.error,
            "log": list(self.log),
            "result": self.result,
        }


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current: Job | None = None
        self.last: Job | None = None
        self._process: subprocess.Popen | None = None

    def start(self, kind: str, command: list[str], result_path: Path) -> Job:
        with self._lock:
            if self.current and self.current.status == "running":
                raise RuntimeError("이미 실행 중인 작업이 있습니다. 끝날 때까지 기다려 주세요.")
            job = Job(id=uuid.uuid4().hex[:10], kind=kind, command=command)
            log_path = LOG_DIR / f"{job.id}-{kind}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(BASE_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env={
                        **os.environ,
                        "PYTHONIOENCODING": "utf-8",
                        "PYTHONUTF8": "1",
                    },
                )
            except OSError as error:
                job.status = "failed"
                job.error = f"{type(error).__name__}: {error}"
                job.finished_at = now_iso()
                self.last = job
                raise RuntimeError(job.error) from error
            self.current = job
            self.last = job
            self._process = process
            thread = threading.Thread(
                target=self._collect,
                args=(job, process, log_path, result_path),
                daemon=True,
            )
            thread.start()
            return job

    def _collect(
        self,
        job: Job,
        process: subprocess.Popen,
        log_path: Path,
        result_path: Path,
    ) -> None:
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            assert process.stdout is not None
            for line in process.stdout:
                text = line.rstrip("\r\n")
                job.log.append(text)
                log.write(text + "\n")
                log.flush()
            returncode = process.wait()
        job.returncode = returncode
        job.finished_at = now_iso()
        if job.cancelled:
            job.status = "cancelled"
        elif returncode == 0:
            job.status = "succeeded"
            job.result = read_json(result_path)
        else:
            job.status = "failed"
            tail = "\n".join(list(job.log)[-12:])
            job.error = tail or f"종료 코드 {returncode}"
        with self._lock:
            if self._process is process:
                self._process = None

    def cancel(self) -> bool:
        with self._lock:
            if not self.current or self.current.status != "running" or not self._process:
                return False
            self.current.cancelled = True
            self._process.terminate()
            return True

    def state(self) -> dict:
        with self._lock:
            job = self.current or self.last
            return {
                "job": job.to_dict() if job else None,
                "running": bool(self.current and self.current.status == "running"),
            }


MANAGER = JobManager()


def _csv_coins() -> list[dict]:
    path = OUTPUT_DIR / "recent-coins-preview.csv"
    if not path.exists():
        return []
    coins: list[dict] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("name") or "").strip()
            symbol = (row.get("symbol") or "").strip()
            slug = (row.get("slug") or "").strip()
            display = (row.get("display_name") or "").strip()
            if not display:
                base = f"{name} ({symbol})" if symbol and not symbol_in_name(name, symbol) else name
                display = f"{base} [{slug}]" if slug else base
            coins.append(
                {
                    "display_name": display,
                    "name": name,
                    "symbol": symbol,
                    "slug": slug,
                    "coin_url": row.get("coin_url") or "",
                    "listed_age": row.get("listed_age") or "",
                    "portfolio_state": row.get("portfolio_state") or "unknown",
                    "page": int(row.get("page") or 0),
                    "duplicate_index": int(row.get("duplicate_index") or 1),
                }
            )
    return coins


def latest_payload() -> dict:
    manifest = read_json(STATE_DIR / "manifest.json")
    records = manifest.get("coins", {}) if isinstance(manifest, dict) else {}
    for candidate in (LAST_RESULT, LAST_SCAN, LAST_FAVORITES):
        payload = read_json(candidate)
        if isinstance(payload, dict) and payload.get("coins"):
            coins = payload["coins"]
            break
    else:
        payload = {}
        coins = _csv_coins()

    favorite_by_url = {}
    if isinstance(payload, dict):
        for item in payload.get("favorites", []):
            favorite_by_url[item.get("coin_url")] = item.get("status")

    merged = []
    for coin in coins:
        record = records.get(coin.get("coin_url"), {}) if isinstance(records, dict) else {}
        item = dict(coin)
        item["capture_status"] = record.get("status")
        item["favorite_status"] = record.get("favorite_status") or favorite_by_url.get(
            coin.get("coin_url")
        )
        merged.append(item)

    return {
        "source": payload.get("mode") if isinstance(payload, dict) else "csv",
        "generated_at": payload.get("finished_at")
        or payload.get("started_at")
        if isinstance(payload, dict)
        else None,
        "coins": merged,
        "captures": payload.get("captures", []) if isinstance(payload, dict) else [],
        "favorites": payload.get("favorites", []) if isinstance(payload, dict) else [],
        "errors": payload.get("errors", []) if isinstance(payload, dict) else [],
        "paths": payload.get("paths", {}) if isinstance(payload, dict) else {},
    }


def _selection_file(urls: list[str]) -> Path:
    write_json(SELECTION, {"urls": urls})
    return SELECTION


def _command(
    mode: str,
    pages: int,
    since_days: int,
    browser: str,
    attach: bool,
    include_unstarred: bool,
    force: bool,
    urls: list[str] | None,
    result_path: Path,
) -> list[str]:
    command = [
        sys.executable,
        "-u",
        str(BASE_DIR / "coingecko_archive.py"),
        "--mode",
        mode,
        "--pages",
        str(pages),
        "--since-days",
        str(since_days),
        "--browser",
        browser,
        "--output-dir",
        str(OUTPUT_DIR),
        "--result-json",
        str(result_path),
    ]
    if attach:
        command.append("--attach")
    if include_unstarred and mode == "capture":
        command.append("--include-unstarred")
    if force:
        command.append("--force")
    if urls is not None:
        command += ["--selection-json", str(_selection_file(urls))]
    return command


def _validate(body: dict) -> tuple[int, int, str, bool, bool, bool, list[str] | None]:
    pages = int(body.get("pages", 10))
    since_days = int(body.get("since_days", 0))
    browser = str(body.get("browser", "chrome"))
    attach = bool(body.get("attach", True))
    include_unstarred = bool(body.get("include_unstarred", False))
    force = bool(body.get("force", False))
    urls = body.get("urls")
    if not 1 <= pages <= 10:
        raise ValueError("페이지 수는 1~10 사이여야 합니다.")
    if not 0 <= since_days <= 30:
        raise ValueError("기간은 요청 페이지 전체 또는 최근 1~30일이어야 합니다.")
    if browser not in {"chrome", "edge"}:
        raise ValueError("브라우저는 chrome 또는 edge만 지원합니다.")
    if urls is not None:
        if not isinstance(urls, list):
            raise ValueError("urls는 문자열 배열이어야 합니다.")
        urls = [str(value) for value in urls if str(value).strip()]
    return pages, since_days, browser, attach, include_unstarred, force, urls


def _start_job(kind: str, mode: str, body: dict) -> dict:
    pages, since_days, browser, attach, include_unstarred, force, urls = _validate(body)
    result_path = {
        "scan": LAST_SCAN,
        "capture": LAST_RESULT,
        "favorites": LAST_FAVORITES,
    }[kind]
    command = _command(
        mode, pages, since_days, browser, attach, include_unstarred, force, urls, result_path
    )
    job = MANAGER.start(kind, command, result_path)
    return {"job": job.to_dict(), "command": command}


def _safe_output_path(relative: str) -> Path | None:
    candidate = (OUTPUT_DIR / relative).resolve()
    try:
        candidate.relative_to(OUTPUT_DIR)
    except ValueError:
        return None
    return candidate if candidate.exists() and candidate.is_file() else None


def _latest_coin_urls() -> set[str] | None:
    """Coin URLs from the newest scan/capture result, or None if unavailable."""
    for candidate in (LAST_RESULT, LAST_SCAN, LAST_FAVORITES):
        payload = read_json(candidate)
        if isinstance(payload, dict) and payload.get("coins"):
            return {
                str(coin.get("coin_url")).rstrip("/").lower()
                for coin in payload["coins"]
                if coin.get("coin_url")
            }
    return None


def build_zip(scope: str = "latest") -> Path:
    """Build a ZIP for the latest listing or the whole archive.

    ``latest`` keeps the download small by including only files tied to the
    most recent scan/capture result. ``all`` includes every manifest file.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    target = STATE_DIR / f"coingecko-archive-{scope}.zip"
    temporary = target.with_suffix(".tmp")
    manifest = read_json(STATE_DIR / "manifest.json")
    records = manifest.get("coins", {}) if isinstance(manifest, dict) else {}
    wanted = _latest_coin_urls() if scope == "latest" else None
    relative_files: set[str] = set()
    for name in (
        "recent-coins-preview.csv",
        "capture-report.txt",
        ".state/manifest.json",
        ".state/web-last-scan.json",
        ".state/web-last-result.json",
        ".state/web-last-favorites.json",
    ):
        if (OUTPUT_DIR / name).exists():
            relative_files.add(name)
    for coin_url, record in (records.items() if isinstance(records, dict) else []):
        if wanted is not None and str(coin_url).rstrip("/").lower() not in wanted:
            continue
        for key in (
            "screenshot",
            "twitter_screenshot",
            "whitepaper_screenshot",
            "pdf",
        ):
            if record.get(key):
                relative_files.add(str(record[key]))
        for item in record.get("explorer_screenshots", []) or []:
            relative_files.add(str(item))

    sources = {
        relative: path
        for relative in relative_files
        if (path := _safe_output_path(relative)) is not None
    }
    if (
        target.exists()
        and sources
        and target.stat().st_mtime >= max(path.stat().st_mtime for path in sources.values())
    ):
        return target

    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for relative in sorted(sources):
                path = sources[relative]
                arcname = relative.replace("\\", "/")
                if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".pdf"}:
                    # Already-compressed media gains nothing from deflate.
                    archive.write(path, arcname=arcname, compress_type=zipfile.ZIP_STORED)
                else:
                    archive.write(
                        path,
                        arcname=arcname,
                        compress_type=zipfile.ZIP_DEFLATED,
                        compresslevel=1,
                    )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


class Handler(BaseHTTPRequestHandler):
    server_version = "CoinGeckoArchiveWeb/2.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Keep the console quiet; the UI owns the job log.
        return

    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path, download_name: str | None = None) -> None:
        if not path.exists() or not path.is_file():
            self._send_json({"error": "파일이 없습니다."}, HTTPStatus.NOT_FOUND)
            return
        data_length = path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", str(data_length))
        if path.suffix.lower() == ".csv":
            self.send_header("Content-Type", "text/csv; charset=utf-8")
        elif path.suffix.lower() == ".zip":
            self.send_header("Content-Type", "application/zip")
        elif path.suffix.lower() == ".txt":
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        else:
            self.send_header("Content-Type", "application/octet-stream")
        if download_name:
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{download_name.encode("ascii", "ignore").decode() or "download"}"',
            )
        self.end_headers()
        with path.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError("JSON 요청 본문이 올바르지 않습니다.") from error
        if not isinstance(data, dict):
            raise ValueError("JSON 객체 형태의 요청 본문이 필요합니다.")
        return data

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            if path == "/":
                data = INDEX_HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/state":
                state = MANAGER.state()
                self._send_json(
                    {
                        **state,
                        "chrome": chrome_status(),
                        "schedule": schedule_status(),
                        "output_dir": str(OUTPUT_DIR),
                    }
                )
            elif path == "/api/latest":
                self._send_json(latest_payload())
            elif path == "/api/chrome":
                self._send_json(chrome_status())
            elif path == "/download/preview.csv":
                self._send_file(
                    OUTPUT_DIR / "recent-coins-preview.csv", "recent-coins-preview.csv"
                )
            elif path == "/download/report.txt":
                self._send_file(OUTPUT_DIR / "capture-report.txt", "capture-report.txt")
            elif path == "/download/result.zip":
                target = build_zip("latest")
                self._send_file(
                    target,
                    f"coingecko-archive-latest-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip",
                )
            elif path == "/download/archive-all.zip":
                target = build_zip("all")
                self._send_file(
                    target,
                    f"coingecko-archive-all-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip",
                )
            else:
                self._send_json({"error": "없는 경로입니다."}, HTTPStatus.NOT_FOUND)
        except Exception as error:
            self._send_json(
                {"error": f"{type(error).__name__}: {error}"}, HTTPStatus.INTERNAL_SERVER_ERROR
            )

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            body = self._read_body()
            if path == "/api/scan":
                self._send_json(_start_job("scan", "dry-run", body))
            elif path == "/api/run":
                self._send_json(_start_job("capture", "capture", body))
            elif path == "/api/favorites":
                self._send_json(_start_job("favorites", "favorites", body))
            elif path == "/api/job/cancel":
                self._send_json({"cancelled": MANAGER.cancel()})
            elif path == "/api/chrome/open-inspect":
                browser = str(body.get("browser", "chrome"))
                executable = _browser_executable(browser)
                if not executable:
                    raise RuntimeError(f"{browser} 실행 파일을 찾지 못했습니다.")
                url = (
                    "edge://inspect/#remote-debugging"
                    if browser == "edge"
                    else "chrome://inspect/#remote-debugging"
                )
                subprocess.Popen(
                    [executable, url],
                    close_fds=True,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
                self._send_json({"opened": True})
            elif path == "/api/schedule/disable":
                result = subprocess.run(
                    ["schtasks", "/Change", "/TN", TASK_NAME, "/DISABLE"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self._send_json(
                    {
                        "returncode": result.returncode,
                        "output": (result.stdout or result.stderr).strip(),
                        "schedule": schedule_status(force=True),
                    }
                )
            elif path == "/api/schedule/enable":
                result = subprocess.run(
                    ["schtasks", "/Change", "/TN", TASK_NAME, "/ENABLE"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self._send_json(
                    {
                        "returncode": result.returncode,
                        "output": (result.stdout or result.stderr).strip(),
                        "schedule": schedule_status(force=True),
                    }
                )
            else:
                self._send_json({"error": "없는 경로입니다."}, HTTPStatus.NOT_FOUND)
        except ValueError as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except RuntimeError as error:
            self._send_json({"error": str(error)}, HTTPStatus.CONFLICT)
        except Exception as error:
            self._send_json(
                {"error": f"{type(error).__name__}: {error}"}, HTTPStatus.INTERNAL_SERVER_ERROR
            )


INDEX_HTML = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>CoinGecko Archive</title>
<style>
  :root {
    --bg: #f4f6fb;
    --card: #ffffff;
    --ink: #14203a;
    --muted: #64748b;
    --line: #e2e8f0;
    --brand: #4338ca;
    --brand-ink: #ffffff;
    --ok: #0f766e;
    --warn: #b45309;
    --danger: #b91c1c;
    --soft: #eef2ff;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font-family: "Segoe UI", "Malgun Gothic", "Apple SD Gothic Neo", system-ui, sans-serif;
    font-size: 14px; line-height: 1.5;
  }
  header {
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
    padding: 22px 28px; background: linear-gradient(120deg, #1e1b4b, #4338ca 62%, #6366f1);
    color: #fff;
  }
  header h1 { margin: 0; font-size: 22px; letter-spacing: -0.02em; }
  header p { margin: 2px 0 0; opacity: .82; font-size: 12.5px; }
  .pills { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
  .pill {
    display: inline-flex; align-items: center; gap: 6px; padding: 6px 11px;
    border-radius: 999px; font-size: 12px; font-weight: 650;
    background: rgba(255,255,255,.14); color: #fff; border: 1px solid rgba(255,255,255,.18);
  }
  .pill.ok { background: #ccfbf1; color: #115e59; border-color: #99f6e4; }
  .pill.warn { background: #fef3c7; color: #92400e; border-color: #fde68a; }
  .pill.danger { background: #fee2e2; color: #991b1b; border-color: #fecaca; }
  main { max-width: 1240px; margin: 0 auto; padding: 22px 20px 60px; }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 16px;
    padding: 18px; margin-bottom: 16px; box-shadow: 0 8px 26px rgba(15, 23, 42, .045);
  }
  .card.warning { border-color: #fcd34d; background: #fffbeb; }
  .card-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
  .card-head h2 { margin: 0; font-size: 15px; letter-spacing: -0.01em; }
  .row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .row + .row { margin-top: 12px; }
  label { display: inline-flex; align-items: center; gap: 7px; color: #334155; font-size: 13px; }
  select, input[type="number"] {
    border: 1px solid #cbd5e1; background: #fff; color: var(--ink);
    border-radius: 9px; padding: 7px 9px; font: inherit;
  }
  input[type="checkbox"] { accent-color: var(--brand); width: 15px; height: 15px; }
  button {
    border: 0; border-radius: 10px; padding: 9px 14px; font: inherit; font-weight: 650;
    cursor: pointer; transition: transform .05s ease, filter .15s ease, background .15s ease;
  }
  button:active { transform: translateY(1px); }
  button:disabled { opacity: .5; cursor: not-allowed; }
  button.primary { background: var(--brand); color: var(--brand-ink); }
  button.primary:hover:not(:disabled) { filter: brightness(1.08); }
  button.secondary { background: var(--soft); color: #3730a3; border: 1px solid #c7d2fe; }
  button.ghost { background: #fff; color: #334155; border: 1px solid #cbd5e1; }
  button.link { background: transparent; color: #4338ca; padding: 4px 6px; text-decoration: underline; text-underline-offset: 3px; }
  .small { font-size: 12.5px; color: var(--muted); }
  .steps { margin: 8px 0 0; padding-left: 20px; color: #78350f; font-size: 13px; }
  .steps li { margin: 3px 0; }
  .steps code { background: #fef3c7; border: 1px solid #fde68a; border-radius: 6px; padding: 1px 6px; }
  .table-wrap { overflow: auto; border: 1px solid var(--line); border-radius: 12px; }
  table { border-collapse: collapse; width: 100%; min-width: 860px; }
  thead th {
    position: sticky; top: 0; background: #f8fafc; color: #475569; text-align: left;
    font-size: 12px; font-weight: 700; padding: 10px 11px; border-bottom: 1px solid var(--line);
    white-space: nowrap;
  }
  tbody td { padding: 9px 11px; border-bottom: 1px solid #f1f5f9; vertical-align: middle; }
  tbody tr:hover { background: #fafaff; }
  tbody tr:last-child td { border-bottom: 0; }
  .coin { font-weight: 650; }
  .symbol { color: #64748b; font-size: 12px; }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11.5px; font-weight: 650;
    background: #f1f5f9; color: #475569; white-space: nowrap;
  }
  .badge.active { background: #ccfbf1; color: #115e59; }
  .badge.inactive { background: #f1f5f9; color: #64748b; }
  .badge.completed { background: #dcfce7; color: #166534; }
  .badge.needs_review { background: #fef3c7; color: #92400e; }
  .badge.failed { background: #fee2e2; color: #991b1b; }
  a { color: #4338ca; text-decoration: none; }
  a:hover { text-decoration: underline; }
  pre {
    margin: 0; padding: 13px; border-radius: 12px; background: #0f172a; color: #dbeafe;
    font-family: "Cascadia Mono", Consolas, monospace; font-size: 12px; line-height: 1.55;
    min-height: 150px; max-height: 360px; overflow: auto; white-space: pre-wrap; word-break: break-word;
  }
  .empty { padding: 28px; text-align: center; color: var(--muted); }
  .toast {
    position: fixed; right: 20px; bottom: 20px; max-width: 460px; padding: 12px 15px;
    border-radius: 12px; background: #111827; color: #fff; box-shadow: 0 12px 30px rgba(0,0,0,.25);
    font-size: 13px; z-index: 50;
  }
  .toast.error { background: #991b1b; }
  .toast.ok { background: #115e59; }
  .hidden { display: none !important; }
  .status-dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; display: inline-block; }
</style>
</head>
<body>
<header>
  <div>
    <h1>CoinGecko Archive</h1>
    <p>수동 실행 전용 · 실사용 Chrome 세션으로 목록/즐겨찾기/다운로드</p>
  </div>
  <div class="pills">
    <span id="chromePill" class="pill"><span class="status-dot"></span>Chrome 확인 중</span>
    <span id="schedulePill" class="pill"><span class="status-dot"></span>예약 확인 중</span>
  </div>
</header>
<main>
  <section id="setupCard" class="card warning hidden">
    <div class="card-head"><h2>실사용 Chrome 연결이 필요합니다</h2><button id="openInspectBtn" class="secondary">Chrome 설정 열기</button></div>
    <ol class="steps">
      <li>열린 Chrome 탭에서 <code>chrome://inspect/#remote-debugging</code> 주소로 이동합니다.</li>
      <li><b>Allow remote debugging for this browser instance</b>를 켭니다.</li>
      <li>이 페이지로 돌아와 <b>목록 새로고침</b>을 누릅니다. Allow를 이미 켰다면 별도 승인 팝업은 뜨지 않을 수 있습니다.</li>
    </ol>
    <p class="small" style="margin:10px 0 0">로그인/즐겨찾기 세션을 그대로 사용합니다. 쿠키를 복사하거나 별도 프로필에 로그인하지 않습니다. 사용이 끝나면 같은 설정을 꺼두세요.</p>
  </section>

  <section class="card">
    <div class="row">
      <label>페이지 <select id="pages"><option>1</option><option>2</option><option>3</option><option>4</option><option>5</option><option selected>10</option></select></label>
      <label>등록 기간 <select id="sinceDays"><option value="0" selected>페이지 전체</option><option value="1">1일</option><option value="3">3일</option><option value="7">7일</option><option value="14">14일</option><option value="30">30일</option></select></label>
      <label class="small"><input type="checkbox" id="includeUnstarred"> 별표 상태와 무관하게 모두 수집</label>
      <label class="small"><input type="checkbox" id="force"> 완료된 코인도 다시 수집</label>
    </div>
    <div class="row">
      <button id="scanBtn" class="secondary">목록 새로고침</button>
      <button id="runBtn" class="primary">원클릭 수집</button>
      <button id="favoriteBtn" class="secondary">선택 즐겨찾기 추가</button>
      <button id="zipBtn" class="ghost">ZIP 다운로드</button>
      <button id="csvBtn" class="ghost">CSV</button>
      <button id="archiveAllBtn" class="link">전체 아카이브 ZIP</button>
    </div>
    <div class="row small">
      <label><input type="checkbox" id="selectAll"> 전체 선택</label>
      <span id="selectionInfo">0개 선택</span>
      <span style="flex:1"></span>
      <button id="disableScheduleBtn" class="link">예약 작업 끄기</button>
      <button id="cancelBtn" class="link hidden">실행 중지</button>
    </div>
  </section>

  <section class="card">
    <div class="card-head"><h2>코인 목록</h2><span id="listMeta" class="small">아직 불러오지 않음</span></div>
    <div class="table-wrap">
      <table>
        <thead><tr><th></th><th>코인</th><th>심볼</th><th>등록</th><th>즐겨찾기</th><th>수집</th><th>페이지</th><th>링크</th></tr></thead>
        <tbody id="coinRows"><tr><td colspan="8" class="empty">목록을 불러오려면 <b>목록 새로고침</b>을 누르세요.</td></tr></tbody>
      </table>
    </div>
  </section>

  <section class="card">
    <div class="card-head"><h2>실행 로그</h2><span id="jobState" class="small">대기 중</span></div>
    <pre id="log">대기 중...</pre>
  </section>
</main>
<div id="toast" class="toast hidden"></div>
<script>
const $ = (id) => document.getElementById(id);
const state = { coins: [], job: null, chrome: null, schedule: null, timer: null };
const esc = (value) => String(value ?? "");

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json().catch(() => ({ error: response.statusText }));
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

let toastTimer = null;
function toast(message, kind = "") {
  const node = $("toast");
  node.textContent = message;
  node.className = "toast" + (kind ? " " + kind : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.add("hidden"), 5200);
}

function setPill(id, text, kind) {
  const node = $(id);
  node.className = "pill" + (kind ? " " + kind : "");
  node.innerHTML = '<span class="status-dot"></span>' + esc(text);
}

function renderChrome(chrome) {
  state.chrome = chrome;
  const ready = chrome && chrome.remote_debugging_enabled && chrome.reachable;
  setPill("chromePill", ready ? "Chrome 디버깅 포트 감지됨" : "Chrome 연결 필요", ready ? "ok" : "warn");
  $("setupCard").classList.toggle("hidden", !!ready);
}

function renderSchedule(schedule) {
  state.schedule = schedule;
  const enabled = schedule && schedule.enabled;
  setPill("schedulePill", enabled ? "예약 작업 켜짐" : "예약 작업 꺼짐", enabled ? "warn" : "ok");
  $("disableScheduleBtn").textContent = enabled ? "예약 작업 끄기" : "예약 작업 켜기";
  $("disableScheduleBtn").dataset.enabled = enabled ? "1" : "0";
}

function renderJob(job) {
  state.job = job;
  const running = job && job.status === "running";
  for (const id of ["scanBtn", "runBtn", "favoriteBtn", "disableScheduleBtn", "openInspectBtn"]) {
    $(id).disabled = running;
  }
  $("cancelBtn").classList.toggle("hidden", !running);
  if (!job) {
    $("jobState").textContent = "대기 중";
    $("log").textContent = "대기 중...";
    return;
  }
  $("jobState").textContent = `${job.kind} · ${job.status}${job.returncode !== null ? " · exit " + job.returncode : ""}`;
  $("log").textContent = (job.log && job.log.length ? job.log.join("\n") : "로그가 아직 없습니다.");
  $("log").scrollTop = $("log").scrollHeight;
}

function badge(text, kind) {
  const span = document.createElement("span");
  span.className = "badge" + (kind ? " " + kind : "");
  span.textContent = text;
  return span;
}

function renderCoins(payload) {
  state.coins = (payload && payload.coins) || [];
  const tbody = $("coinRows");
  tbody.textContent = "";
  if (!state.coins.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 8;
    cell.className = "empty";
    cell.textContent = "표시할 코인이 없습니다. 목록 새로고침을 실행하세요.";
    row.appendChild(cell);
    tbody.appendChild(row);
  }
  for (const coin of state.coins) {
    const row = document.createElement("tr");

    const checkCell = document.createElement("td");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = coin.coin_url;
    checkbox.className = "coin-check";
    checkbox.addEventListener("change", updateSelection);
    checkCell.appendChild(checkbox);
    row.appendChild(checkCell);

    const nameCell = document.createElement("td");
    const name = document.createElement("div");
    name.className = "coin";
    name.textContent = coin.display_name || coin.name || "(이름 없음)";
    nameCell.appendChild(name);
    row.appendChild(nameCell);

    const symbolCell = document.createElement("td");
    symbolCell.className = "symbol";
    symbolCell.textContent = coin.symbol || "-";
    row.appendChild(symbolCell);

    const ageCell = document.createElement("td");
    ageCell.textContent = coin.listed_age || "-";
    row.appendChild(ageCell);

    const portfolioCell = document.createElement("td");
    const portfolio = coin.portfolio_state || "unknown";
    portfolioCell.appendChild(
      badge(portfolio === "active" ? "★ 추가됨" : portfolio === "inactive" ? "☆ 없음" : "? 확인 불가",
        portfolio === "active" ? "active" : portfolio === "inactive" ? "inactive" : "")
    );
    row.appendChild(portfolioCell);

    const captureCell = document.createElement("td");
    const capture = coin.capture_status || "-";
    captureCell.appendChild(
      badge(capture, capture === "completed" ? "completed" : capture === "needs_review" ? "needs_review" : "")
    );
    row.appendChild(captureCell);

    const pageCell = document.createElement("td");
    pageCell.textContent = coin.page || "-";
    row.appendChild(pageCell);

    const linkCell = document.createElement("td");
    const link = document.createElement("a");
    link.href = coin.coin_url;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = "CoinGecko";
    linkCell.appendChild(link);
    row.appendChild(linkCell);

    tbody.appendChild(row);
  }
  const completed = state.coins.filter((coin) => coin.capture_status === "completed").length;
  const review = state.coins.filter((coin) => coin.capture_status === "needs_review").length;
  $("listMeta").textContent = `${state.coins.length}개 · 완료 ${completed} · 검토 ${review}`;
  $("selectAll").checked = false;
  updateSelection();
}

function updateSelection() {
  const checked = document.querySelectorAll(".coin-check:checked").length;
  $("selectionInfo").textContent = `${checked}개 선택`;
}

function selectedUrls() {
  return [...document.querySelectorAll(".coin-check:checked")].map((node) => node.value);
}

function requestBody() {
  return {
    pages: Number($("pages").value),
    since_days: Number($("sinceDays").value),
    include_unstarred: $("includeUnstarred").checked,
    force: $("force").checked,
    attach: true,
    browser: "chrome",
  };
}

async function startJob(path, body) {
  try {
    const data = await api(path, { method: "POST", body: JSON.stringify(body) });
    renderJob(data.job);
    toast("작업을 시작했습니다. 로그에서 실사용 Chrome 연결 상태를 확인하세요.", "ok");
    startPolling();
  } catch (error) {
    toast(error.message, "error");
  }
}

function startPolling() {
  if (state.timer) return;
  state.timer = setInterval(refreshState, 1400);
}

function stopPolling() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
}

async function refreshState() {
  try {
    const data = await api("/api/state");
    renderChrome(data.chrome);
    renderSchedule(data.schedule);
    renderJob(data.job);
    if (!data.running) {
      stopPolling();
      await loadLatest();
    }
  } catch (error) {
    stopPolling();
    toast(error.message, "error");
  }
}

async function loadLatest() {
  try {
    const payload = await api("/api/latest");
    if (payload.coins && payload.coins.length) renderCoins(payload);
  } catch (error) {
    // A missing list is normal before the first run.
  }
}

$("scanBtn").addEventListener("click", () => startJob("/api/scan", requestBody()));
$("runBtn").addEventListener("click", () => {
  const urls = selectedUrls();
  startJob("/api/run", { ...requestBody(), urls: urls.length ? urls : null });
});
$("favoriteBtn").addEventListener("click", () => {
  const urls = selectedUrls();
  if (!urls.length) {
    toast("즐겨찾기에 추가할 코인을 먼저 선택하세요.", "error");
    return;
  }
  if (!confirm(`선택한 ${urls.length}개 코인을 CoinGecko 즐겨찾기에 추가할까요?`)) return;
  startJob("/api/favorites", { ...requestBody(), urls });
});
$("selectAll").addEventListener("change", (event) => {
  document.querySelectorAll(".coin-check").forEach((node) => { node.checked = event.target.checked; });
  updateSelection();
});
$("zipBtn").addEventListener("click", () => { window.location.href = "/download/result.zip"; });
$("csvBtn").addEventListener("click", () => { window.location.href = "/download/preview.csv"; });
$("archiveAllBtn").addEventListener("click", () => {
  if (!confirm("전체 아카이브는 수백 MB가 될 수 있고 ZIP 생성에 시간이 걸립니다. 계속할까요?")) return;
  window.location.href = "/download/archive-all.zip";
});
$("cancelBtn").addEventListener("click", async () => {
  try {
    const data = await api("/api/job/cancel", { method: "POST", body: "{}" });
    toast(data.cancelled ? "중지 요청을 보냈습니다." : "중지할 작업이 없습니다.");
  } catch (error) { toast(error.message, "error"); }
});
$("openInspectBtn").addEventListener("click", async () => {
  try {
    await api("/api/chrome/open-inspect", { method: "POST", body: JSON.stringify({ browser: "chrome" }) });
    toast("Chrome 설정 탭을 열었습니다. 원격 디버깅을 켜주세요.", "ok");
  } catch (error) { toast(error.message, "error"); }
});
$("disableScheduleBtn").addEventListener("click", async () => {
  const enabled = $("disableScheduleBtn").dataset.enabled === "1";
  const path = enabled ? "/api/schedule/disable" : "/api/schedule/enable";
  if (enabled && !confirm("자동 예약 작업을 끌까요? 수동 웹 버튼만 사용하게 됩니다.")) return;
  try {
    const data = await api(path, { method: "POST", body: "{}" });
    renderSchedule(data.schedule);
    toast(enabled ? "예약 작업을 껐습니다." : "예약 작업을 켰습니다.", "ok");
  } catch (error) { toast(error.message, "error"); }
});

(async function init() {
  await refreshState();
  await loadLatest();
})();
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Local CoinGecko Archive web control panel")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--output-dir", help="Archive output directory")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.output_dir:
        configure(Path(args.output_dir))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{args.port}/"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"CoinGecko Archive 웹 UI: {url}")
    print(f"출력 폴더: {OUTPUT_DIR}")
    print("종료하려면 이 창에서 Ctrl+C를 누르세요.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n웹 UI를 종료합니다.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
