"""One-time userscript install links (E16).

An admin asks for a link for one Arena profile. The link works once and expires after
`INSTALL_LINK_TTL_SECONDS` (10 minutes). Only a SHA-256 of the link token is kept, and the store is
in memory: a link does not survive a backend restart, which is the intended behaviour for a
short-lived credential.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass

INSTALL_LINK_TTL_SECONDS = 600


@dataclass(frozen=True)
class _Link:
    profile_id: str
    actor: str
    expires_at: float


class InstallLinkStore:
    def __init__(self, ttl_seconds: int = INSTALL_LINK_TTL_SECONDS, clock=time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._links: dict[str, _Link] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _prune(self, now: float) -> None:
        for key in [k for k, link in self._links.items() if link.expires_at <= now]:
            del self._links[key]

    def create(self, profile_id: str, actor: str) -> tuple[str, int]:
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = self._clock()
            self._prune(now)
            self._links[self._key(token)] = _Link(profile_id=profile_id, actor=actor, expires_at=now + self._ttl)
        return token, self._ttl

    def consume(self, token: str) -> _Link | None:
        """Return the link once; a second call, or a call after expiry, returns None."""
        key = self._key(token or "")
        with self._lock:
            now = self._clock()
            self._prune(now)
            link = self._links.pop(key, None)
        return link


install_links = InstallLinkStore()
