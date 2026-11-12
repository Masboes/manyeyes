"""Who is calling: GitHub sign-in (browser), API tokens (scripts, MCP), allowlist.

Every request re-checks the allowlist, so removing a name from the file
revokes that person's sessions and tokens on the next request.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import HTTPException, Request

if TYPE_CHECKING:
    from ..config import Settings
    from ..store.db import Store

SESSION_COOKIE = "manyeyes_session"
STATE_COOKIE = "manyeyes_oauth_state"
SESSION_TTL_S = 7 * 24 * 3600
TOKEN_PREFIX = "me_"
GH_AUTHORIZE = "https://github.com/login/oauth/authorize"
GH_TOKEN = "https://github.com/login/oauth/access_token"
GH_USER = "https://api.github.com/user"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_api_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


class Allowlist:
    """GitHub logins from a file, one per line; reloaded when the file changes."""

    def __init__(self, path: Path, store: Store | None = None) -> None:
        self.path = path
        self.store = store
        self._mtime: float | None = None
        self._names: frozenset[str] = frozenset()

    def names(self) -> frozenset[str]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._mtime:
            old = self._names
            new: set[str] = set()
            if mtime is not None:
                for line in self.path.read_text().splitlines():
                    line = line.split("#", 1)[0].strip()
                    if line:
                        new.add(line.lower())
            self._names, self._mtime = frozenset(new), mtime
            if self.store is not None and old != self._names:
                self.store.audit(None, "allowlist_reload", {
                    "added": sorted(self._names - old), "removed": sorted(old - self._names)})
        return self._names

    def allows(self, login: str | None) -> bool:
        return bool(login) and login.lower() in self.names()


@dataclass
class Caller:
    login: str
    via: str   # session | token | bootstrap | mcp-oauth


class Auth:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.s = settings
        self.store = store
        self.allowlist = Allowlist(settings.allowlist_file, store)
        self.session_secret = settings["auth.session_secret"] or self._stored_secret()

    def _stored_secret(self) -> str:
        """A random secret generated once into the data dir (mode 600)."""
        path = self.s.data_dir / "session_secret"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass   # another process created it first
            else:
                with os.fdopen(fd, "w") as fh:
                    fh.write(secrets.token_urlsafe(48))
        return path.read_text().strip()

    @property
    def github_enabled(self) -> bool:
        return bool(self.s["auth.github_client_id"] and self.s["auth.github_client_secret"])

    # -- sessions ---------------------------------------------------------

    def issue_session(self, login: str) -> str:
        now = int(time.time())
        return jwt.encode({"sub": login, "iat": now, "exp": now + SESSION_TTL_S,
                           "aud": "manyeyes-web"}, self.session_secret, algorithm="HS256")

    def session_login(self, token: str) -> str | None:
        try:
            data = jwt.decode(token, self.session_secret, algorithms=["HS256"],
                              audience="manyeyes-web")
        except jwt.PyJWTError:
            return None
        login = data.get("sub")
        return login if self.allowlist.allows(login) else None

    # -- tokens -----------------------------------------------------------

    def token_login(self, token: str) -> tuple[str, str] | None:
        """(login, via) for a valid API or bootstrap token."""
        boot = self.s["auth.bootstrap_token"]
        if boot and hmac.compare_digest(token.encode(), boot.encode()):
            login = self.s["auth.bootstrap_login"]
            return (login, "bootstrap") if self.allowlist.allows(login) else None
        if not token.startswith(TOKEN_PREFIX):
            return None
        row = self.store.token_by_hash(hash_token(token))
        if row is None or not self.allowlist.allows(row["github_login"]):
            return None
        self.store.touch_token(row["id"])
        return row["github_login"], "token"

    # -- request ----------------------------------------------------------

    def caller(self, request: Request) -> Caller | None:
        header = request.headers.get("authorization", "")
        if header[:7].lower() == "bearer ":
            hit = self.token_login(header[7:].strip())
            return Caller(*hit) if hit else None
        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie:
            login = self.session_login(cookie)
            if login:
                return Caller(login, "session")
        return None

    def require(self, request: Request) -> Caller:
        c = self.caller(request)
        if c is None:
            raise HTTPException(401, "sign in, or send a valid API token")
        return c

    # -- GitHub OAuth (browser) ---------------------------------------------

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        return GH_AUTHORIZE + "?" + urlencode({
            "client_id": self.s["auth.github_client_id"], "redirect_uri": redirect_uri,
            "state": state, "allow_signup": "false",
        })

    async def exchange(self, code: str, redirect_uri: str) -> str | None:
        """Trade the OAuth code for the GitHub login; the GitHub token is discarded."""
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(GH_TOKEN, headers={"Accept": "application/json"}, data={
                "client_id": self.s["auth.github_client_id"],
                "client_secret": self.s["auth.github_client_secret"],
                "code": code, "redirect_uri": redirect_uri})
            access = (r.json() if r.status_code == 200 else {}).get("access_token")
            if not access:
                return None
            me = await c.get(GH_USER, headers={"Authorization": f"Bearer {access}",
                                               "Accept": "application/vnd.github+json"})
        return (me.json() or {}).get("login") if me.status_code == 200 else None
