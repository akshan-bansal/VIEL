"""API authentication, role-based authorisation and rate limiting (spec §15, §14).

Keys are supplied through ``VIA_API_KEYS`` (JSON list of {key, principal, roles}).
Roles: reader, ingestor, admin (propose reference-data changes), approver
(approve them). Maker-checker is enforced on principals, not on keys.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)
ROLES = ("reader", "ingestor", "admin", "approver")


@dataclass(frozen=True)
class Principal:
    name: str
    roles: frozenset[str]


class KeyStore:
    def __init__(self, entries: list[dict]):
        self._keys: list[tuple[str, Principal]] = []
        for entry in entries:
            roles = frozenset(entry.get("roles", []))
            unknown = roles - set(ROLES)
            if unknown:
                raise ValueError(f"unknown roles {sorted(unknown)}")
            if not entry.get("key") or not entry.get("principal"):
                raise ValueError("each API key needs 'key' and 'principal'")
            self._keys.append((entry["key"], Principal(entry["principal"], roles)))

    @classmethod
    def from_env(cls) -> KeyStore:
        raw = os.environ.get("VIA_API_KEYS", "[]")
        return cls(json.loads(raw))

    def lookup(self, key: str | None) -> Principal | None:
        if not key:
            return None
        for stored, principal in self._keys:
            if hmac.compare_digest(stored, key):
                return principal
        return None


class RateLimiter:
    """Token bucket per principal."""

    def __init__(self, per_minute: int):
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, who: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(who, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens < 1.0:
                self._buckets[who] = (tokens, now)
                return False
            self._buckets[who] = (tokens - 1.0, now)
            return True


def authenticated(request: Request, key: str | None = Depends(API_KEY_HEADER)) -> Principal:
    principal = request.app.state.keys.lookup(key)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid API key")
    if not request.app.state.rate_limiter.allow(principal.name):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
    return principal


def require(role: str):
    def dependency(principal: Principal = Depends(authenticated)) -> Principal:
        if role not in principal.roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"role '{role}' required")
        return principal

    return dependency
