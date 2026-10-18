"""Security primitives for the demo deployment.

Model: one shared ACCESS_CODE gates the web app. A correct code earns an HttpOnly, signed,
expiring cookie that carries a random per-browser client id. Every cooking session is owned
by the principal that created it, so two code-holders cannot see each other's sessions.
Programmatic MCP clients use a separate static bearer key (MCP_API_KEY) and get their own
principal. Both secrets live only in server environment variables."""
import base64
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable, Mapping
from urllib.parse import urlsplit

from starlette.exceptions import HTTPException

COOKIE_NAME = "ts_auth"


class ConfigError(RuntimeError):
    """Raised at startup when required security configuration is missing or too weak."""


def _csv(value: str | None, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    items = tuple(v.strip().lower() for v in (value or "").split(",") if v.strip())
    return items or default


def _bool(value: str | None, default: bool) -> bool:
    return default if value is None or value == "" else value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    app_env: str
    access_code: str
    session_secret: str
    mcp_api_key: str | None
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    cookie_secure: bool
    cookie_ttl_s: int
    chat_per_min: int
    state_per_min: int
    login_per_min: int
    global_chat_per_hour: int
    turn_timeout_s: float
    max_body_bytes: int


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    app_env = (env.get("APP_ENV") or "production").strip().lower()
    if app_env not in ("production", "development"):
        raise ConfigError("APP_ENV must be 'production' or 'development'.")
    prod = app_env == "production"

    code = env.get("ACCESS_CODE", "")
    if len(code) < (12 if prod else 6):
        raise ConfigError("ACCESS_CODE is required (at least 12 characters in production). "
                          "Generate one: python -c \"import secrets; print(secrets.token_urlsafe(16))\"")

    secret = env.get("SESSION_SECRET", "")
    if len(secret) < 32:
        if prod:
            raise ConfigError("SESSION_SECRET is required in production (at least 32 characters). "
                              "Generate one: python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
        secret = secrets.token_urlsafe(32)  # development only: sign-ins reset on every restart

    mcp_key = env.get("MCP_API_KEY") or None
    if mcp_key and len(mcp_key) < 24:
        raise ConfigError("MCP_API_KEY must be at least 24 characters (or leave it unset to disable /mcp/).")

    return Settings(
        app_env=app_env,
        access_code=code,
        session_secret=secret,
        mcp_api_key=mcp_key,
        allowed_hosts=_csv(env.get("ALLOWED_HOSTS"), ("localhost", "127.0.0.1")),
        allowed_origins=_csv(env.get("ALLOWED_ORIGINS")),
        cookie_secure=_bool(env.get("COOKIE_SECURE"), prod),
        cookie_ttl_s=int(env.get("COOKIE_TTL_HOURS", "24")) * 3600,
        chat_per_min=int(env.get("CHAT_PER_MIN", "12")),
        state_per_min=int(env.get("STATE_PER_MIN", "120")),
        login_per_min=int(env.get("LOGIN_PER_MIN", "5")),
        global_chat_per_hour=int(env.get("GLOBAL_CHAT_PER_HOUR", "300")),
        turn_timeout_s=float(env.get("TURN_TIMEOUT_S", "45")),
        max_body_bytes=int(env.get("MAX_BODY_BYTES", "16384")),
    )


# ------------------------------------------------------------------- cookies
def _sig(settings: Settings, payload: str) -> str:
    digest = hmac.new(settings.session_secret.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def issue_cookie(settings: Settings, client_id: str | None = None, now: float | None = None) -> str:
    client_id = client_id or secrets.token_urlsafe(16)
    expires = int((time.time() if now is None else now) + settings.cookie_ttl_s)
    payload = f"{client_id}.{expires}"
    return f"{payload}.{_sig(settings, payload)}"


def verify_cookie(settings: Settings, value: str | None, now: float | None = None) -> str | None:
    """Returns the client id if the cookie is authentic and unexpired, else None."""
    if not value or len(value) > 200:
        return None
    parts = value.split(".")
    if len(parts) != 3 or not parts[0] or not parts[1].isdigit():
        return None
    payload = f"{parts[0]}.{parts[1]}"
    if not hmac.compare_digest(_sig(settings, payload), parts[2]):
        return None
    if int(parts[1]) < (time.time() if now is None else now):
        return None
    return parts[0]


def codes_match(supplied: str, expected: str) -> bool:
    """Constant-time comparison that does not leak length."""
    return hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(), hashlib.sha256(expected.encode()).digest())


# ------------------------------------------------------------- rate limiting
class RateLimiter:
    """Sliding-window limiter. In-memory and per process: run a single worker (see DEPLOY.md)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, limit: int, window_s: float = 60.0) -> bool:
        now, window = time.monotonic(), self._hits[key]
        while window and now - window[0] > window_s:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(now)
        if len(self._hits) > 20000:  # bound memory under key-spraying
            for k in [k for k, v in self._hits.items() if not v]:
                del self._hits[k]
        return True

    def clear(self) -> None:
        self._hits.clear()


def origin_allowed(settings: Settings, origin: str | None, host: str | None) -> bool:
    """CSRF defence for state-changing requests. A browser always sends Origin on cross-site POSTs."""
    if origin is None:
        return True  # non-browser clients; they still need the cookie, which SameSite=Strict withholds cross-site
    if origin.lower() in settings.allowed_origins:
        return True
    try:
        netloc = urlsplit(origin).netloc.lower()
    except ValueError:
        return False
    return bool(netloc) and netloc == (host or "").lower()


# --------------------------------------------------------------- ASGI layers
class SecurityHeadersMiddleware:
    CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
           "connect-src 'self'; media-src 'self'; object-src 'none'; base-uri 'none'; "
           "form-action 'self'; frame-ancestors 'none'")

    def __init__(self, app, get_settings: Callable[[], Settings]) -> None:
        self.app, self.get_settings = app, get_settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        hsts = self.get_settings().cookie_secure  # secure cookies imply the site is served over HTTPS

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                extra = {
                    b"content-security-policy": self.CSP.encode(),
                    b"x-content-type-options": b"nosniff",
                    b"x-frame-options": b"DENY",
                    b"referrer-policy": b"no-referrer",
                    b"permissions-policy": b"microphone=(self), camera=(), geolocation=()",
                    b"cross-origin-opener-policy": b"same-origin",
                }
                if path.startswith("/api/") or path.startswith("/mcp"):
                    extra[b"cache-control"] = b"no-store"
                if hsts:
                    extra[b"strict-transport-security"] = b"max-age=31536000"
                have = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in extra.items() if k not in have]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _TooLarge(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=413, detail="Request too large.")


class BodyLimitMiddleware:
    """Rejects oversized request bodies before they are buffered, including chunked uploads."""

    def __init__(self, app, get_settings: Callable[[], Settings]) -> None:
        self.app, self.get_settings = app, get_settings

    async def _reject(self, send):
        body = b'{"detail":"Request too large."}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        max_bytes = self.get_settings().max_body_bytes
        declared = dict(scope["headers"]).get(b"content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            return await self._reject(send)
        received, started = 0, False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    raise _TooLarge()
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _TooLarge:
            if not started:
                await self._reject(send)


class McpAuthMiddleware:
    """Requires `Authorization: Bearer <MCP_API_KEY>` on /mcp. With no key configured the endpoint is disabled."""

    def __init__(self, app, get_settings: Callable[[], Settings], limiter: RateLimiter, per_min: int = 60) -> None:
        self.app, self.get_settings, self.limiter, self.per_min = app, get_settings, limiter, per_min

    async def _respond(self, send, status: int, detail: str, extra=()):
        body = ('{"detail":"%s"}' % detail).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), *extra]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/mcp" or path.startswith("/mcp/")):
            return await self.app(scope, receive, send)
        api_key = self.get_settings().mcp_api_key
        if not api_key:
            return await self._respond(send, 404, "Not found.")
        client = (scope.get("client") or ("unknown", 0))[0]
        if not self.limiter.allow(f"mcp:{client}", self.per_min * 5):
            return await self._respond(send, 429, "Too many requests.")
        supplied = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
        token = supplied[7:] if supplied.lower().startswith("bearer ") else ""
        if not token or not codes_match(token, api_key):
            if not self.limiter.allow(f"mcp-fail:{client}", self.per_min):
                return await self._respond(send, 429, "Too many requests.")
            return await self._respond(send, 401, "Unauthorized.", [(b"www-authenticate", b"Bearer")])
        await self.app(scope, receive, send)
