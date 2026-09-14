"""Shared HTTP plumbing: retries, disk caching, and polite rate limiting."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from ..config import get_config
from ..util import stable_id


class SourceError(RuntimeError):
    """A source could not be fetched. Callers degrade rather than crash."""


class HttpClient:
    """Thin httpx wrapper with retry/backoff and an optional on-disk cache.

    The cache is what keeps a 15-minute refresh loop from hammering free
    endpoints, and it is also what lets the whole app run from recorded
    responses in tests.
    """

    def __init__(
        self,
        *,
        timeout: float | None = None,
        cache_ttl: float = 0.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        cfg = get_config()
        self.timeout = timeout if timeout is not None else cfg.http_timeout
        self.cache_ttl = cache_ttl
        self.cache_dir: Path = cfg.cache_dir
        self.headers = {"User-Agent": cfg.user_agent, **(headers or {})}
        self._last_call: dict[str, float] = {}

    # ------------------------------------------------------------- caching
    def _cache_path(self, url: str, params: dict | None) -> Path:
        return self.cache_dir / f"{stable_id(url, json.dumps(params or {}, sort_keys=True))}.json"

    def _read_cache(self, path: Path, ttl: float) -> Any | None:
        if ttl <= 0 or not path.exists():
            return None
        if time.time() - path.stat().st_mtime > ttl:
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def _write_cache(self, path: Path, payload: Any) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload))
        except (OSError, TypeError):
            pass

    # -------------------------------------------------------------- fetch
    def get_json(
        self,
        url: str,
        params: dict | None = None,
        *,
        cache_ttl: float | None = None,
        retries: int = 3,
        min_interval: float = 0.0,
    ) -> Any:
        ttl = self.cache_ttl if cache_ttl is None else cache_ttl
        cache_path = self._cache_path(url, params)
        cached = self._read_cache(cache_path, ttl)
        if cached is not None:
            return cached

        host = httpx.URL(url).host or url
        if min_interval:
            elapsed = time.monotonic() - self._last_call.get(host, 0.0)
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)

        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                with httpx.Client(timeout=self.timeout, headers=self.headers, follow_redirects=True) as c:
                    resp = c.get(url, params=params)
                self._last_call[host] = time.monotonic()
                if resp.status_code == 429:
                    raise SourceError(f"rate limited by {host}")
                resp.raise_for_status()
                payload = resp.json()
                self._write_cache(cache_path, payload)
                return payload
            except Exception as exc:  # noqa: BLE001 - any transport error retries
                last_error = exc
                if attempt < retries - 1:
                    time.sleep(2**attempt)

        # A stale cache entry beats no data at all when a source goes down.
        stale = self._read_cache(cache_path, ttl=float("inf"))
        if stale is not None:
            return stale
        raise SourceError(f"GET {url} failed: {last_error}")

    def get_text(
        self,
        url: str,
        params: dict | None = None,
        *,
        retries: int = 3,
    ) -> str:
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                with httpx.Client(timeout=self.timeout, headers=self.headers, follow_redirects=True) as c:
                    resp = c.get(url, params=params)
                resp.raise_for_status()
                return resp.text
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < retries - 1:
                    time.sleep(2**attempt)
        raise SourceError(f"GET {url} failed: {last_error}")

    def download(self, url: str, dest: Path, *, cache_ttl: float = 86400.0) -> Path:
        """Fetch a binary file (parquet/csv), reusing a fresh local copy."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and time.time() - dest.stat().st_mtime < cache_ttl:
            return dest
        try:
            with httpx.Client(timeout=max(self.timeout, 120.0), headers=self.headers,
                              follow_redirects=True) as c:
                with c.stream("GET", url) as resp:
                    resp.raise_for_status()
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    with tmp.open("wb") as fh:
                        for chunk in resp.iter_bytes(65536):
                            fh.write(chunk)
                    tmp.replace(dest)
        except Exception as exc:  # noqa: BLE001
            if dest.exists():
                return dest  # stale is better than nothing
            raise SourceError(f"download {url} failed: {exc}") from exc
        return dest
