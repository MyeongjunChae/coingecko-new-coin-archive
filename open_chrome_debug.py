"""Start Chrome with the debugging port so no "Allow" dialog is needed.

Chrome 136+ can expose remote debugging through
``chrome://inspect/#remote-debugging``, but the browser then asks the user to
allow each connecting tool. Launching Chrome with ``--remote-debugging-port``
instead writes ``DevToolsActivePort`` at startup and never asks again, while
still using the real profile (so the CoinGecko login is kept).

Chrome only honours the flag when it is not already running, so this helper
closes the running browser first (tabs are restored on the next start).

Examples
--------
    python collector\\open_chrome_debug.py            # status only
    python collector\\open_chrome_debug.py --launch   # close Chrome, relaunch with the port
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

COLLECTOR = Path(__file__).resolve().parent
if str(COLLECTOR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR))

from coingecko_archive import (  # noqa: E402
    _browser_executable,
    browser_user_data_dir,
    read_devtools_active_port,
)

DEFAULT_PORT = 9222


def _reachable(endpoint: str | None, timeout: float = 2.0) -> bool:
    if not endpoint:
        return False
    rest = endpoint[len("ws://") :]
    hostport, _, _path = rest.partition("/")
    host, _, port = hostport.partition(":")
    try:
        with socket.create_connection((host, int(port or 80)), timeout=timeout):
            return True
    except OSError:
        return False


def _serves_json(endpoint: str | None, timeout: float = 3.0) -> bool:
    """True when /json/version answers.

    A browser started with ``--remote-debugging-port`` serves the HTTP
    endpoints. The ``chrome://inspect`` mode hides them (404) and asks the user
    to allow every connecting client, which is what we want to avoid.
    """
    if not endpoint:
        return False
    rest = endpoint[len("ws://") :]
    hostport, _, _path = rest.partition("/")
    host, _, port = hostport.partition(":")
    try:
        with socket.create_connection((host, int(port or 80)), timeout=timeout) as sock:
            sock.settimeout(timeout)
            request = (
                f"GET /json/version HTTP/1.1\r\nHost: {hostport}\r\n"
                "Connection: close\r\n\r\n"
            )
            sock.sendall(request.encode("ascii"))
            reply = sock.recv(200)
    except OSError:
        return False
    return b" 200 " in reply.split(b"\r\n", 1)[0]


def _session_kind(endpoint: str | None) -> str:
    if not _reachable(endpoint):
        return "none"
    return "port" if _serves_json(endpoint) else "inspect"


def _chrome_processes(browser: str) -> list[int]:
    name = {"edge": "msedge.exe"}.get(browser, "chrome.exe")
    try:
        output = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in output.splitlines():
        parts = [item.strip('"') for item in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == name:
            try:
                pids.append(int(parts[1]))
            except ValueError:
                continue
    return pids


def _close_browser(browser: str, wait_seconds: float = 20.0) -> bool:
    """Ask the browser to close, then force-close anything left over."""
    pids = _chrome_processes(browser)
    if not pids:
        return True
    print(f"{browser} 창을 닫습니다 ({len(pids)}개 프로세스)...")
    name = {"edge": "msedge"}.get(browser, "chrome")
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Get-Process {name} -ErrorAction SilentlyContinue | "
            "ForEach-Object { $_.CloseMainWindow() | Out-Null }",
        ],
        capture_output=True,
        timeout=60,
    )
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if not _chrome_processes(browser):
            return True
        time.sleep(1.0)
    print("정상 종료되지 않아 강제 종료합니다.")
    subprocess.run(
        ["taskkill", "/F", "/IM", {"edge": "msedge.exe"}.get(browser, "chrome.exe")],
        capture_output=True,
        timeout=60,
    )
    time.sleep(2.0)
    return not _chrome_processes(browser)


def _launch(browser: str, profile: Path, port: int | None) -> bool:
    executable = _browser_executable(browser)
    if not executable:
        print(f"{browser} 실행 파일을 찾지 못했습니다.")
        return False
    stale = profile / "DevToolsActivePort"
    if stale.exists():
        stale.unlink(missing_ok=True)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    if port is None:
        print(f"{browser}를 일반 모드로 실행합니다.")
        command = [executable, "--restore-last-session"]
    else:
        print(f"{browser}를 디버깅 포트 {port}로 실행합니다.")
        command = [
            executable,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--restore-last-session",
        ]
    subprocess.Popen(command, close_fds=True, creationflags=flags)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", default="chrome", choices=("chrome", "edge"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--launch", action="store_true")
    parser.add_argument(
        "--normal",
        action="store_true",
        help="디버깅 플래그 없이 브라우저를 다시 실행합니다(chrome://inspect 방식 복구).",
    )
    parser.add_argument("--keep-open", action="store_true", help="실행 중인 브라우저를 닫지 않습니다.")
    args = parser.parse_args()

    profile = browser_user_data_dir(args.browser)
    if args.normal:
        if not args.keep_open and not _close_browser(args.browser):
            print("브라우저를 닫지 못했습니다. 직접 종료한 뒤 다시 실행하세요.")
            return 2
        if not _launch(args.browser, profile, None):
            return 3
        print("일반 모드로 실행했습니다.")
        print("이제 chrome://inspect/#remote-debugging 에서 Allow 스위치를 한 번 켜면 됩니다.")
        return 0

    endpoint = read_devtools_active_port(profile)
    kind = _session_kind(endpoint)
    if kind == "port":
        print(f"이미 디버깅 포트로 실행 중입니다: {endpoint}")
        return 0
    if kind == "inspect":
        print("chrome://inspect 방식 세션이 감지되었습니다(연결마다 Allow 필요).")
        if not args.launch:
            print("실행: python collector\\open_chrome_debug.py --launch")
            return 1

    if not args.launch:
        print(f"디버깅 세션이 없습니다. 프로필: {profile}")
        print("실행: python collector\\open_chrome_debug.py --launch")
        return 1

    if not args.keep_open:
        if not _close_browser(args.browser):
            print("브라우저를 닫지 못했습니다. 직접 종료한 뒤 다시 실행하세요.")
            return 2

    if not _launch(args.browser, profile, args.port):
        return 3

    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(1.0)
        endpoint = read_devtools_active_port(profile)
        if _session_kind(endpoint) == "port":
            print(f"준비되었습니다: {endpoint}")
            print("이제 수집/즐겨찾기 작업을 실행하면 Allow 창 없이 연결됩니다.")
            return 0
    print("디버깅 포트가 열리지 않았습니다. DevToolsActivePort 파일을 확인하세요:")
    print(f"  {profile / 'DevToolsActivePort'}")
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
