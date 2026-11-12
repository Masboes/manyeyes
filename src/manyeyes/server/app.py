"""The server: web UI, REST API, MCP and GitHub sign-in in one process.

Behind a reverse proxy under a prefix (for example /manyeyes/), the proxy
strips the prefix and sends X-Forwarded-Prefix; every URL the server builds
(OAuth callbacks, cookie paths, links) puts the prefix back.
"""
from __future__ import annotations

import hashlib
import html
import ipaddress
import logging
import re
import secrets
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..store.db import Store
from .api import create_router
from .auth import SESSION_COOKIE, SESSION_TTL_S, STATE_COOKIE, Auth
from .mcp import create_mcp
from .queue import Worker

if TYPE_CHECKING:
    from ..config import Settings

log = logging.getLogger("manyeyes.server")


class ServerError(RuntimeError):
    pass


def _trusted(settings: Settings, host: str | None) -> bool:
    nets = settings["server.trusted_proxies"]
    if "*" in nets:
        return True
    if not host:
        return False
    if host in nets:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    for n in nets:
        try:
            if addr in ipaddress.ip_network(n, strict=False):
                return True
        except ValueError:
            continue
    return False


def external_base(settings: Settings, request: Request) -> str:
    """The public base URL of this request, prefix included, no trailing slash."""
    proto, host, prefix = request.url.scheme, request.headers.get("host", ""), ""
    if _trusted(settings, request.client.host if request.client else None):
        proto = request.headers.get("x-forwarded-proto", proto).split(",")[0].strip()
        host = request.headers.get("x-forwarded-host", host).split(",")[0].strip()
        prefix = request.headers.get("x-forwarded-prefix", "")
    prefix = (prefix or settings["server.root_path"] or "").rstrip("/")
    if prefix and not prefix.startswith("/"):
        prefix = "/" + prefix
    return f"{proto}://{host}{prefix}"


def checked_base(settings: Settings, request: Request) -> str:
    """external_base, but only if it is one of server.public_urls (when set)."""
    base = external_base(settings, request)
    allowed = [u.rstrip("/") for u in settings["server.public_urls"]]
    if allowed and base not in allowed:
        raise HTTPException(400, f"{base} is not a configured public URL")
    return base


def create_app(settings: Settings) -> FastAPI:
    store = Store(settings.db_path)
    auth = Auth(settings, store)
    if not auth.github_enabled and not settings["auth.bootstrap_token"]:
        raise ServerError("no way to sign in: configure the GitHub OAuth App "
                          "(MANYEYES_GITHUB_CLIENT_ID/_SECRET) or a bootstrap token")
    if not settings.allowlist_file.exists():
        log.warning("allowlist %s does not exist: nobody can sign in", settings.allowlist_file)
    worker = Worker(settings, store)
    mcp = create_mcp(settings, store, worker, auth)
    mcp_app = mcp.http_app(path="/")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings["jail.enabled"]:
            from ..engine import jail
            leaks = await jail.self_test(settings.data_dir)
            if leaks:
                raise ServerError(f"jail self-test failed, refusing to start: {leaks}")
            log.info("jail self-test passed")
        worker.start()
        async with mcp_app.lifespan(app):
            yield
        await worker.stop()

    app = FastAPI(title="manyeyes", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    app.state.store, app.state.worker, app.state.auth = store, worker, auth

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    # -- GitHub sign-in ---------------------------------------------------------

    def cookie_path(base: str) -> str:
        return (urlparse(base).path or "") + "/"

    @app.get("/auth/config")
    def auth_config(request: Request) -> dict:
        c = auth.caller(request)
        base = external_base(settings, request)
        return {"github": auth.github_enabled, "login": c.login if c else None,
                "version": __version__, "mcp_url": f"{base}/mcp", "base": base}

    @app.get("/auth/login")
    def login(request: Request) -> RedirectResponse:
        if not auth.github_enabled:
            raise HTTPException(404, "GitHub sign-in is not configured")
        base = checked_base(settings, request)
        state = secrets.token_urlsafe(24)
        resp = RedirectResponse(auth.authorize_url(f"{base}/auth/callback", state))
        resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True,
                        secure=base.startswith("https"), samesite="lax",
                        path=cookie_path(base) + "auth")
        return resp

    @app.get("/auth/callback")
    async def callback(request: Request, code: str = "", state: str = ""):
        base = checked_base(settings, request)
        expected = request.cookies.get(STATE_COOKIE, "")
        if not code or not state or not expected or not secrets.compare_digest(
                state.encode(), expected.encode()):
            raise HTTPException(400, "invalid sign-in state; start again")
        login_name = await auth.exchange(code, f"{base}/auth/callback")
        if not auth.allowlist.allows(login_name):
            store.audit(login_name, "login_denied", {})
            return HTMLResponse(_page("Access denied",
                                      f"The GitHub account <b>{html.escape(login_name or '?')}</b> "
                                      "is not on "
                                      "this server's allowlist."), status_code=403)
        store.touch_user(login_name)
        store.audit(login_name, "login", {})
        resp = RedirectResponse(f"{base}/")
        resp.set_cookie(SESSION_COOKIE, auth.issue_session(login_name), max_age=SESSION_TTL_S,
                        httponly=True, secure=base.startswith("https"), samesite="lax",
                        path=cookie_path(base))
        resp.delete_cookie(STATE_COOKIE, path=cookie_path(base) + "auth")
        return resp

    @app.post("/auth/logout")
    def logout(request: Request) -> JSONResponse:
        base = external_base(settings, request)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path=cookie_path(base))
        return resp

    app.include_router(create_router(settings, store, worker, auth))

    # -- web UI -------------------------------------------------------------------
    # Assets live under static/<content hash>/: every change gets new URLs, so
    # browsers and CDNs may cache them forever, and a stale cached entry (even
    # a cached 404) can never block an update. Relative references inside the
    # CSS and JS inherit the hashed directory.
    web = Path(str(resources.files("manyeyes.server") / "web"))
    # The whole package goes into the hash: any deploy with a change gets new
    # asset URLs, so nothing a CDN cached earlier can stick.
    asset_hash = _tree_hash(Path(str(resources.files("manyeyes"))))
    page = ((web / "index.html").read_text()
            .replace("__VERSION__", __version__)
            .replace('"static/', f'"static/{asset_hash}/'))

    @app.get("/", include_in_schema=False)
    def index() -> Response:
        return HTMLResponse(page, headers={"Cache-Control": "no-cache"})

    app.mount(f"/static/{asset_hash}", ImmutableStatic(directory=str(web)), name="static")
    # MCP and its OAuth routes (/mcp/authorize, /mcp/token, ...) live under /mcp.
    app.mount("/mcp", mcp_app)
    return PathShim(app)  # type: ignore[return-value]


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in root.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts):
        h.update(str(f.relative_to(root)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


class ImmutableStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if resp.status_code == 200:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "no-store"   # never let a CDN keep a 404
        return resp


class PathShim:
    """Path fixes in front of the app, without redirects.

    - `/mcp` is served as `/mcp/`: a redirect would make clients drop the
      Authorization header (and behind TLS offload it can even downgrade to http).
    - OAuth discovery documents live at the ORIGIN root (RFC 9728 / RFC 8414),
      with the resource path appended: `/.well-known/oauth-protected-resource/<prefix>/mcp`.
      The proxy forwards those unchanged; here they map to MCP's own handlers.
    """

    WELL_KNOWN = ("/.well-known/oauth-protected-resource",
                  "/.well-known/oauth-authorization-server",
                  "/.well-known/openid-configuration")

    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.state = app.state

    def __getattr__(self, name: str):
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope["path"]
            if path == "/mcp":
                scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
            else:
                for wk in self.WELL_KNOWN:
                    if path == wk or path.startswith(wk + "/"):
                        rest = path[len(wk):].rstrip("/") if wk.endswith("resource") else ""
                        new = "/mcp" + wk + rest
                        scope = {**scope, "path": new, "raw_path": new.encode()}
                        break
        await self.app(scope, receive, send)


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><meta charset=utf-8><title>{title}</title>"
            "<body style='font-family:system-ui;max-width:32rem;margin:15vh auto;padding:0 1rem'>"
            f"<h1>{title}</h1><p>{body}</p></body>")


class _RedactQuery(logging.Filter):
    """Hide OAuth codes and state in access-log lines (they arrive as query strings)."""

    PATTERN = re.compile(r"((?:code|state)=)[^&\s\"]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self.PATTERN.sub(r"\1***", a) if isinstance(a, str) else a
                                for a in record.args)
        return True


def serve(settings: Settings) -> int:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("uvicorn.access").addFilter(_RedactQuery())
    app = create_app(settings)
    uvicorn.run(app, host=settings["server.host"], port=int(settings["server.port"]),
                proxy_headers=False, log_level="info")
    return 0
