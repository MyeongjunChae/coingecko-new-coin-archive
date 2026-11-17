"""Keep one Chrome DevTools connection alive and share it with local clients.

Chrome 136+ asks the user to allow remote debugging *for every connecting
client* when the browser is started normally. A tool that connects once per
job therefore forces the user to click "Allow" over and over.

This relay connects to the live browser exactly once, then serves that single
upstream connection on ``127.0.0.1`` so Playwright can attach as many times as
it wants without any further dialog. Clients are served one at a time, which is
all a single-user archive tool needs.

Usage
-----
    python collector\\cdp_relay.py --start     # start in the background
    python collector\\cdp_relay.py --status
    python collector\\cdp_relay.py --stop
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import select
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

COLLECTOR = Path(__file__).resolve().parent
if str(COLLECTOR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR))

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_PORT = 9223
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CoinGeckoArchive"
LOG_PATH = STATE_DIR / "cdp-relay.log"
PID_PATH = STATE_DIR / "cdp-relay.pid"
CONNECTED_PATH = STATE_DIR / "cdp-relay.connected"

# Chrome keeps the websocket upgrade pending while the consent dialog waits for
# a click, so give the user plenty of time instead of re-asking on every retry.
UPSTREAM_TIMEOUT = 300.0


# ---------------------------------------------------------------- WebSocket --
class WsError(RuntimeError):
    pass


class WsConnection:
    """Small WebSocket wrapper that can act as client or server."""

    def __init__(self, sock: socket.socket, *, client_side: bool) -> None:
        self.sock = sock
        self.client_side = client_side
        self.buffer = b""
        self._send_lock = threading.Lock()

    # -- framing --------------------------------------------------------
    def _read_exact(self, count: int) -> bytes:
        while len(self.buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WsError("connection closed")
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        size = len(payload)
        mask_bit = 0x80 if self.client_side else 0x00
        if size < 126:
            header.append(mask_bit | size)
        elif size < 65536:
            header.append(mask_bit | 126)
            header += struct.pack(">H", size)
        else:
            header.append(mask_bit | 127)
            header += struct.pack(">Q", size)
        if self.client_side:
            mask = os.urandom(4)
            header += mask
            payload = bytes(
                value ^ mask[index % 4] for index, value in enumerate(payload)
            )
        with self._send_lock:
            self.sock.sendall(bytes(header) + payload)

    def recv_text(self) -> str:
        first, second = self._read_exact(2)
        opcode = first & 0x0F
        size = second & 0x7F
        if size == 126:
            (size,) = struct.unpack(">H", self._read_exact(2))
        elif size == 127:
            (size,) = struct.unpack(">Q", self._read_exact(8))
        if second & 0x80:
            mask = self._read_exact(4)
            data = bytes(
                value ^ mask[index % 4]
                for index, value in enumerate(self._read_exact(size))
            )
        else:
            data = self._read_exact(size)
        if opcode == 0x8:
            raise WsError("closed by peer")
        if opcode == 0x9:
            self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
            return self.recv_text()
        return data.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _read_request(sock: socket.socket) -> tuple[dict[str, str], bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise WsError("handshake closed")
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1", "replace").split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name:
            headers[name.strip().lower()] = value.strip()
    return headers, rest


def upstream_host(endpoint: str) -> tuple[str, int, str]:
    rest = endpoint[len("ws://") :] if endpoint.startswith("ws://") else endpoint
    hostport, _, path = rest.partition("/")
    host, _, port = hostport.partition(":")
    return host, int(port or 80), "/" + path


def connect_upstream(endpoint: str, timeout: float = UPSTREAM_TIMEOUT) -> WsConnection:
    """Open the single upstream connection (Chrome asks for consent here)."""
    host, port, path = upstream_host(endpoint)
    sock = socket.create_connection((host, port), timeout=10.0)
    sock.settimeout(timeout)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade,Keep-Alive\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(request.encode("ascii"))
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            raise WsError("DevTools closed during handshake")
        head += chunk
    header, _, rest_bytes = head.partition(b"\r\n\r\n")
    status = header.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    if " 101 " not in status:
        raise WsError(f"DevTools handshake failed: {status}")
    connection = WsConnection(sock, client_side=True)
    connection.buffer = rest_bytes
    sock.settimeout(None)
    return connection


def accept_client(server: socket.socket) -> WsConnection:
    sock, _ = server.accept()
    sock.settimeout(30.0)
    headers, rest = _read_request(sock)
    key = headers.get("sec-websocket-key", "")
    accept = base64.b64encode(
        hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
    ).decode("ascii")
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    )
    sock.sendall(response.encode("ascii"))
    sock.settimeout(None)
    connection = WsConnection(sock, client_side=False)
    connection.buffer = rest
    return connection


# --------------------------------------------------------------- relay core --
def _next_message(connection: WsConnection) -> str:
    return connection.recv_text()


def relay_client(client: WsConnection, upstream: WsConnection, sessions: set[str]) -> str:
    """Forward frames both ways until one side dies.

    Returns ``"client"`` when the local client disconnected (the shared
    upstream is still healthy) and ``"upstream"`` when Chrome went away.
    """
    while True:
        watch: list[socket.socket] = []
        if client.buffer:
            watch.append(client.sock)
        if upstream.buffer:
            watch.append(upstream.sock)
        if not watch:
            try:
                ready, _, _ = select.select([client.sock, upstream.sock], [], [], 1.0)
            except OSError:
                return "client"
            watch = list(ready)
            if not watch:
                continue
        for sock in watch:
            if sock is client.sock:
                try:
                    message = _next_message(client)
                except (WsError, OSError):
                    return "client"
                upstream.send_text(message)
            else:
                try:
                    message = _next_message(upstream)
                except (WsError, OSError):
                    return "upstream"
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict):
                    session_id = payload.get("sessionId")
                    result = payload.get("result")
                    if isinstance(result, dict) and result.get("sessionId"):
                        session_id = result["sessionId"]
                    if isinstance(session_id, str) and session_id:
                        sessions.add(session_id)
                try:
                    client.send_text(message)
                except OSError:
                    return "client"


def _drain(upstream: WsConnection, pending: set[int], timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while pending:
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        try:
            ready, _, _ = select.select([upstream.sock], [], [], remaining)
        except OSError:
            return
        if not ready:
            return
        try:
            message = upstream.recv_text()
        except (WsError, OSError):
            return
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("id") in pending:
            pending.discard(payload["id"])


def detach_sessions(upstream: WsConnection, sessions: set[str]) -> None:
    """Drop the dead client's flattened sessions so the next client starts clean."""
    pending: set[int] = set()
    for index, session_id in enumerate(sorted(sessions)):
        message_id = 900001 + index
        pending.add(message_id)
        try:
            upstream.send_text(
                json.dumps(
                    {
                        "id": message_id,
                        "method": "Target.detachFromTarget",
                        "params": {"sessionId": session_id},
                    }
                )
            )
        except OSError:
            sessions.clear()
            return
    _drain(upstream, pending)
    sessions.clear()


def _is_listening(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def serve(endpoint: str, port: int = DEFAULT_PORT) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(4)
    print(f"relay listening on 127.0.0.1:{port}", flush=True)
    try:
        PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass

    upstream: WsConnection | None = None
    while True:
        if upstream is None:
            print("connecting to the live browser (Allow once if Chrome asks)...", flush=True)
            try:
                upstream = connect_upstream(endpoint)
                print("upstream connected", flush=True)
                CONNECTED_PATH.write_text(
                    time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8"
                )
            except (OSError, WsError) as error:
                print(f"upstream connect failed: {type(error).__name__}: {error}", flush=True)
                upstream = None
                CONNECTED_PATH.unlink(missing_ok=True)
                time.sleep(3.0)
                continue
        try:
            client = accept_client(server)
        except (OSError, WsError) as error:
            print(f"client handshake failed: {type(error).__name__}", flush=True)
            continue
        sessions: set[str] = set()
        try:
            cause = relay_client(client, upstream, sessions)
        except Exception as error:  # noqa: BLE001 - keep the relay alive
            print(f"relay error: {type(error).__name__}: {error}", flush=True)
            cause = "upstream"
        client.close()
        print(f"client disconnected ({cause})", flush=True)
        if cause == "upstream":
            upstream.close()
            upstream = None
            CONNECTED_PATH.unlink(missing_ok=True)
            continue
        detach_sessions(upstream, sessions)


def relay_url(port: int = DEFAULT_PORT) -> str | None:
    if not _is_listening(port):
        return None
    return f"ws://127.0.0.1:{port}/devtools/browser/relay"


def _endpoint(browser: str = "chrome") -> str | None:
    from coingecko_archive import chrome_debug_endpoint

    return chrome_debug_endpoint(browser)


def start_background(port: int = DEFAULT_PORT, endpoint: str | None = None) -> bool:
    if _is_listening(port):
        return True
    endpoint = endpoint or _endpoint()
    if not endpoint:
        print("DevToolsActivePort 가 없습니다. chrome://inspect 에서 Allow 를 켜세요.")
        return False
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_file = open(LOG_PATH, "ab")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--serve",
            "--port",
            str(port),
            "--endpoint",
            endpoint,
        ],
        stdout=log_file,
        stderr=log_file,
        close_fds=True,
        creationflags=flags,
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        if _is_listening(port):
            print(f"relay ready: {relay_url(port)}")
            print("Chrome에 Allow 창이 떴다면 '한 번만' 눌러 주세요.")
            return True
        time.sleep(0.5)
    print(f"relay did not start; see the log: {LOG_PATH}")
    return False


def stop_background() -> bool:
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            check=False,
            capture_output=True,
        )
    except OSError:
        return False
    PID_PATH.unlink(missing_ok=True)
    deadline = time.time() + 5
    while time.time() < deadline:
        if not _is_listening(DEFAULT_PORT):
            return True
        time.sleep(0.2)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--endpoint", default=None)
    args = parser.parse_args()

    if args.serve:
        endpoint = args.endpoint or _endpoint()
        if not endpoint:
            print("DevToolsActivePort 가 없습니다.")
            return 2
        try:
            serve(endpoint, args.port)
        except KeyboardInterrupt:
            return 0
        return 0
    if args.status:
        url = relay_url(args.port)
        print(url or "not running")
        return 0 if url else 1
    if args.stop:
        print("stopped" if stop_background() else "not running")
        return 0
    return 0 if start_background(args.port) else 1


if __name__ == "__main__":
    raise SystemExit(main())
