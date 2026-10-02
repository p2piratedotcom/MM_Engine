from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen


_wallet_proxy_configured = False
_wallet_proxy_url: str | None = None


def configure_wallet_proxy(proxy_url: str | None) -> None:
    """Select the wallet's explicit network route before worker threads start.

    An unreachable proxy raises an error; no direct-network retry is made.
    Local KDF and engine RPC remain direct loopback connections.
    """
    global _wallet_proxy_configured, _wallet_proxy_url
    if proxy_url is not None:
        parsed = urlparse(proxy_url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or not parsed.port or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("wallet proxy must be an HTTP proxy on 127.0.0.1")
    _wallet_proxy_url = proxy_url
    _wallet_proxy_configured = True


def _is_loopback(url: str) -> bool:
    return urlparse(url).hostname in {"127.0.0.1", "localhost", "::1"}


def _open(request: Request, *, timeout: float):
    if not _wallet_proxy_configured:
        return urlopen(request, timeout=timeout)
    proxy = None if _is_loopback(request.full_url) else _wallet_proxy_url
    opener = build_opener(ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))
    return opener.open(request, timeout=timeout)


@dataclass(slots=True)
class TransportError(Exception):
    message: str
    status: int | None = None
    payload: Any = None

    def __str__(self) -> str:
        suffix = f" (HTTP {self.status})" if self.status is not None else ""
        return f"{self.message}{suffix}"


class JsonTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 10.0,
        total_timeout: float | None = None,
    ) -> Any: ...


class UrllibJsonTransport:
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 10.0,
        total_timeout: float | None = None,
    ) -> Any:
        if total_timeout is not None:
            if total_timeout <= 0:
                raise ValueError("total_timeout must be positive")
            return asyncio.run(self._request_with_deadline(
                method=method, url=url, headers=headers, body=body,
                total_timeout=min(timeout, total_timeout),
            ))
        request = Request(url, data=body, headers=dict(headers or {}), method=method)
        try:
            with _open(request, timeout=timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read()
            raise TransportError(
                "remote API rejected the request",
                status=exc.code,
                payload=_decode_payload(raw),
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            # Do not include exception text: it may contain a signed URL/token.
            cause = getattr(exc, 'reason', exc)
            kind = 'timeout' if isinstance(cause, TimeoutError) else type(cause).__name__
            raise TransportError(f"remote API is unreachable ({kind})") from exc
        return _decode_payload(raw)

    async def _request_with_deadline(
        self, *, method: str, url: str, headers: Mapping[str, str] | None,
        body: bytes | None, total_timeout: float,
    ) -> Any:
        # urllib's socket timeout applies separately to connection and reads.
        # aiohttp's total deadline includes both, even when the server stalls.
        import aiohttp

        try:
            deadline = aiohttp.ClientTimeout(total=total_timeout)
            async with aiohttp.ClientSession(timeout=deadline) as session:
                async with session.request(
                    method, url, headers=dict(headers or {}), data=body,
                    allow_redirects=False,
                    proxy=(None if _is_loopback(url) else _wallet_proxy_url)
                    if _wallet_proxy_configured else None,
                ) as response:
                    raw = await response.read()
                    if response.status >= 400 or 300 <= response.status < 400:
                        raise TransportError(
                            "remote API rejected the request",
                            status=response.status,
                            payload=_decode_payload(raw),
                        )
                    return _decode_payload(raw)
        except asyncio.TimeoutError as exc:
            raise TransportError("remote API is unreachable (timeout)") from exc
        except aiohttp.ClientError as exc:
            raise TransportError(
                f"remote API is unreachable ({type(exc).__name__})"
            ) from exc


def _decode_payload(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", errors="replace")
