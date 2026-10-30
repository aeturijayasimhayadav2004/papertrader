"""Polite HTTP for the desks: standard library only, retries, timeouts, no secrets.

Every desk takes a `fetch` callable (url -> bytes) so tests run offline on
fixtures. `default_fetch` is the real one. NSE's JSON API needs the cookies its
home page sets, so `NSESession` visits the home page first.

Free public endpoints change and rate-limit; callers must treat any exception as
"data unavailable", never as a reason to crash a run.
"""
from __future__ import annotations

import gzip
import http.cookiejar
import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

log = logging.getLogger(__name__)

Fetch = Callable[[str], bytes]

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
NSE_HOME = "https://www.nseindia.com"


class FetchError(RuntimeError):
    pass


def _read(resp) -> bytes:
    body = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip" or body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return body


def default_fetch(url: str, headers: dict[str, str] | None = None, timeout: float = 15.0,
                  retries: int = 2, pause: float = 1.5, opener=None) -> bytes:
    hdrs = {"User-Agent": BROWSER_UA, "Accept": "*/*", "Accept-Encoding": "gzip"}
    hdrs.update(headers or {})
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with (opener.open(req, timeout=timeout) if opener else urllib.request.urlopen(req, timeout=timeout)) as r:
                return _read(r)
        except Exception as exc:  # network, HTTP 4xx/5xx, timeouts
            last = exc
            if attempt < retries:
                time.sleep(pause * (attempt + 1))
    raise FetchError(f"{url.split('?')[0]}: {last}") from last


def get_json(url: str, fetch: Fetch | None = None) -> Any:
    return json.loads((fetch or default_fetch)(url).decode("utf-8", "replace"))


def url_with(base: str, **params: Any) -> str:
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    return f"{base}?{q}" if q else base


class NSESession:
    """Fetches www.nseindia.com/api/... with the cookies its home page sets.

    NSE blocks some cloud IPs; expect FetchError there and fall back to other sources.
    Usable as a `Fetch`: NSESession()(url) -> bytes.
    """

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self._primed = False

    def _prime(self) -> None:
        if not self._primed:
            default_fetch(NSE_HOME, {"Accept": "text/html"}, self.timeout, retries=1, opener=self.opener)
            self._primed = True

    def __call__(self, url: str) -> bytes:
        self._prime()
        return default_fetch(url, {"Accept": "application/json", "Referer": NSE_HOME + "/"},
                             self.timeout, retries=1, opener=self.opener)
