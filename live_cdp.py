"""Raw Chrome DevTools Protocol helpers for the live (user) browser session.

Playwright's ``connect_over_cdp`` initializes *every* existing tab of the
attached browser. When Chrome has a tab whose renderer no longer answers (for
example a page frozen/discarded by Memory Saver, or a tab stuck behind a stuck
script), that initialization never finishes and the whole collection run dies
with ``Timeout 45000ms exceeded`` even though the browser itself is healthy.

This module speaks the protocol directly so the collector can:

* list the live tabs,
* find the ones that do not answer,
* wake them up (activate / reload) before Playwright attaches.

It only uses the standard library, so no extra dependency is required.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class CdpError(RuntimeError):
    """Raised when the DevTools WebSocket cannot be used."""


class CdpConnection:
    """Minimal client for a single CDP WebSocket endpoint."""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        parts = urlsplit(url)
        if parts.scheme != "ws":
            raise CdpError(f"unsupported endpoint: {url}")
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 80
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._buffer = b""
        self._next_id = 0
        self._handshake(host, port, path)

    # -- WebSocket plumbing ---------------------------------------------
    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise CdpError("DevTools endpoint closed during handshake")
            head += chunk
        header, _, rest = head.partition(b"\r\n\r\n")
        status = header.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if " 101 " not in status:
            raise CdpError(f"DevTools handshake failed: {status}")
        self._buffer = rest

    def _read_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise CdpError("DevTools endpoint closed the connection")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def _send_text(self, payload: str) -> None:
        data = payload.encode("utf-8")
        header = bytearray([0x81])
        size = len(data)
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", size)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", size)
        mask = os.urandom(4)
        header += mask
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
        self._sock.sendall(bytes(header) + masked)

    def _read_text(self) -> str:
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
            raise CdpError("DevTools endpoint closed the connection")
        if opcode == 0x9:  # ping
            self._sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
            return self._read_text()
        return data.decode("utf-8", "replace")

    # -- CDP requests ---------------------------------------------------
    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        self._next_id += 1
        message_id = self._next_id
        payload: dict[str, Any] = {
            "id": message_id,
            "method": method,
            "params": params or {},
        }
        if session_id:
            payload["sessionId"] = session_id
        self._send_text(json.dumps(payload))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no CDP reply for {method}")
            self._sock.settimeout(remaining)
            message = json.loads(self._read_text())
            if message.get("id") == message_id:
                if "error" in message:
                    raise CdpError(f"{method}: {message['error'].get('message')}")
                return message

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


_PROBE_EXPRESSION = (
    "({ready: document.readyState, url: location.href, title: document.title})"
)


class _LiveSession:
    """One DevTools connection reused for every tab inspection.

    Chrome shows the "allow remote debugging" consent per connecting client per
    browser session, so opening a connection per tab would spray dialogs at the
    user. A connection is only replaced after a tab stops answering, because a
    pending reply makes the socket unusable.
    """

    def __init__(self, endpoint: str, timeout: float) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.client: CdpConnection | None = None

    def _ensure(self) -> CdpConnection:
        if self.client is None:
            self.client = CdpConnection(self.endpoint, timeout=self.timeout)
        return self.client

    def reset(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self._ensure().call(
            method,
            params,
            session_id=session_id,
            timeout=self.timeout if timeout is None else timeout,
        )

    def answers(self, target_id: str) -> tuple[bool, str]:
        """Attach to one tab and ask it for its readyState."""
        try:
            attached = self.call(
                "Target.attachToTarget", {"targetId": target_id, "flatten": True}
            )
            session_id = attached["result"]["sessionId"]
            reply = self.call(
                "Runtime.evaluate",
                {"expression": _PROBE_EXPRESSION, "returnByValue": True},
                session_id=session_id,
            )
            value = reply.get("result", {}).get("result", {}).get("value")
            ready = value.get("ready") if isinstance(value, dict) else "?"
            return True, str(ready)
        except TimeoutError:
            # The reply may still arrive later, so drop the socket.
            self.reset()
            return False, "TimeoutError"
        except (CdpError, OSError, KeyError, json.JSONDecodeError) as error:
            return False, type(error).__name__

    def close(self) -> None:
        self.reset()


def prepare_live_session(
    endpoint: str,
    *,
    probe_timeout: float = 6.0,
    action_timeout: float = 8.0,
) -> dict[str, Any]:
    """Wake unresponsive tabs so Playwright can attach to the browser.

    Returns a report describing what was found and what was attempted. The
    function never raises for browser-side problems; callers treat a failed
    preparation as "try Playwright anyway".
    """
    report: dict[str, Any] = {
        "ok": False,
        "pages": 0,
        "unresponsive": [],
        "revived": [],
        "remaining": [],
    }
    session = _LiveSession(endpoint, probe_timeout)
    try:
        targets = session.call("Target.getTargets", timeout=probe_timeout + 15)
        pages = [
            info
            for info in targets.get("result", {}).get("targetInfos", [])
            if info.get("type") == "page"
        ]
        report["pages"] = len(pages)
        report["ok"] = True
    except (TimeoutError, CdpError, OSError, json.JSONDecodeError) as error:
        report["error"] = f"{type(error).__name__}: {error}"
        session.close()
        return report

    for info in pages:
        target_id = info.get("targetId")
        if not target_id:
            continue
        answered, detail = session.answers(target_id)
        if answered:
            continue
        entry = {
            "targetId": target_id,
            "url": str(info.get("url", ""))[:300],
            "title": str(info.get("title", ""))[:120],
            "detail": detail,
        }
        report["unresponsive"].append(entry)

        # Try to revive: activating a frozen tab usually lets it run again,
        # and reloading rebuilds a discarded renderer.
        revived = False
        try:
            session.call(
                "Target.activateTarget", {"targetId": target_id}, timeout=action_timeout
            )
            revived, _ = session.answers(target_id)
            if not revived:
                attached = session.call(
                    "Target.attachToTarget",
                    {"targetId": target_id, "flatten": True},
                    timeout=action_timeout,
                )
                session.call(
                    "Page.reload",
                    {"ignoreCache": False},
                    session_id=attached["result"]["sessionId"],
                    timeout=action_timeout,
                )
                time.sleep(1.0)
                revived, _ = session.answers(target_id)
        except (TimeoutError, CdpError, OSError, KeyError) as error:
            entry["action_error"] = type(error).__name__
        if revived:
            report["revived"].append(entry)
        else:
            report["remaining"].append(entry)

    session.close()
    return report
